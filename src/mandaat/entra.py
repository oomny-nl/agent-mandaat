"""Entra Agent ID-variant: tokens voor de agent via de agent-OBO-flow, en claimnormalisatie.

Unit-tests met gemockte responses; op 19-10-2026 end-to-end gemeten in een lab-tenant (zie de
README, "Keycloak en Entra naast elkaar"). Protocol volgens
learn.microsoft.com/entra/agent-id/agent-on-behalf-of-oauth-flow (geraadpleegd 17-10-2026):

1. Managed identity-token met audience `api://AzureADTokenExchange` (geen geheim).
2. Blueprint-token T1: `client_credentials` met `fmi_path` = de agent identity en het
   managed identity-token als `client_assertion`.
3. Resource-token: de agent identity doet `jwt-bearer` met `requested_token_use=on_behalf_of`,
   het gebruikerstoken als `assertion` (audience = de blueprint) en T1 als `client_assertion`.

Verschil met Keycloak: Entra zet de delegatie niet in `act`. De agent staat in `azp` (v1: `appid`),
de medewerker in `oid`, en de facetclaims `xms_act_fct` en `xms_sub_fct` geven de rollen aan
(learn.microsoft.com/entra/agent-id/agent-token-claims, geraadpleegd 18-10-2026).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx2

UITWISSEL_AUDIENCE = "api://AzureADTokenExchange"
AGENT_IDENTITY = "11"  # facetwaarde AgentIdentity
AGENT_GEBRUIKER = "13"  # facetwaarde AgentIDUser: het eigen gebruikersaccount van een agent
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"


class EntraFout(RuntimeError):
    """Entra weigerde een tokenverzoek; de tekst bevat de AADSTS-foutcode."""


@dataclass(frozen=True)
class EntraAgent:
    """Configuratie van één agent identity.

    Attributes:
        tenant_id: Tenant van de agent.
        blueprint_id: client_id van de agent identity blueprint.
        agent_id: client_id van de agent identity.
        managed_identity_client_id: client_id van de user-assigned managed identity.
    """

    tenant_id: str
    blueprint_id: str
    agent_id: str
    managed_identity_client_id: str

    @property
    def token_endpoint(self) -> str:
        """Token-endpoint van de tenant."""
        return f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"


def _post(url: str, data: dict[str, str]) -> dict[str, Any]:
    antwoord = httpx2.post(url, data=data, timeout=30)
    if antwoord.status_code != 200:
        raise EntraFout(f"tokenverzoek geweigerd ({antwoord.status_code}): {antwoord.text[:300]}")
    return antwoord.json()


def managed_identity_token(client_id: str) -> str:
    """Managed identity-token voor de token exchange (Container Apps, App Service of IMDS)."""
    endpoint, header = os.environ.get("IDENTITY_ENDPOINT"), os.environ.get("IDENTITY_HEADER")
    params = {"resource": UITWISSEL_AUDIENCE, "client_id": client_id}
    if endpoint and header:
        antwoord = httpx2.get(
            endpoint,
            params={**params, "api-version": "2019-08-01"},
            headers={"X-IDENTITY-HEADER": header},
            timeout=10,
        )
    else:
        antwoord = httpx2.get(
            "http://169.254.169.254/metadata/identity/oauth2/token",
            params={**params, "api-version": "2018-02-01"},
            headers={"Metadata": "true"},
            timeout=10,
        )
    if antwoord.status_code != 200:
        raise EntraFout(f"managed identity-token niet beschikbaar: {antwoord.text[:200]}")
    return antwoord.json()["access_token"]


def blueprint_token(agent: EntraAgent, mi_token: str) -> str:
    """Stap 2: T1, het token van de blueprint dat namens deze agent identity mag uitwisselen."""
    return _post(
        agent.token_endpoint,
        {
            "client_id": agent.blueprint_id,
            "scope": f"{UITWISSEL_AUDIENCE}/.default",
            "fmi_path": agent.agent_id,
            "client_assertion_type": ASSERTION_TYPE,
            "client_assertion": mi_token,
            "grant_type": "client_credentials",
        },
    )["access_token"]


def namens_medewerker(agent: EntraAgent, t1: str, gebruikerstoken: str, scope: str) -> str:
    """Stap 3: resource-token van de agent identity namens de medewerker (agent-OBO).

    Args:
        agent: Configuratie van de agent.
        t1: Blueprint-token uit stap 2.
        gebruikerstoken: Token van de medewerker voor `api://<blueprint>/access_agent` (aud = de
            blueprint).
        scope: Eén gedelegeerde scope, bijvoorbeeld `api://<dossier_api_app_id>/dossier.lezen`.

    Returns:
        Het access token voor de dossier-API.
    """
    return _post(
        agent.token_endpoint,
        {
            "client_id": agent.agent_id,
            "scope": scope,
            "client_assertion_type": ASSERTION_TYPE,
            "client_assertion": t1,
            "grant_type": JWT_BEARER,
            "assertion": gebruikerstoken,
            "requested_token_use": "on_behalf_of",
        },
    )["access_token"]


@dataclass(frozen=True)
class Delegatie:
    """Providerneutrale weergave van een gedelegeerd token."""

    medewerker: str
    agent: str
    scopes: frozenset[str]


def normaliseer_keycloak(claims: dict[str, Any]) -> Delegatie:
    """Keycloak: delegatie staat in `act` (RFC 8693)."""
    actor = claims.get("act")
    if not isinstance(actor, dict) or "client_id" not in actor:
        raise ValueError("geen gedelegeerd token: act ontbreekt")
    return Delegatie(claims["sub"], actor["client_id"], frozenset(str(claims["scope"]).split()))


def _facetten(claims: dict[str, Any], naam: str) -> frozenset[str]:
    """Waarden van een facetclaim: spatiegescheiden, volgorde zonder betekenis."""
    waarde = claims.get(naam)
    return frozenset(waarde.split()) if isinstance(waarde, str) else frozenset()


def normaliseer_entra(claims: dict[str, Any], toegelaten_agents: frozenset[str]) -> Delegatie:
    """Entra Agent ID, agent-OBO namens een mens: agent in `azp`, medewerker in `oid`.

    Alleen dat scenario is een delegatie. Geweigerd worden: een actor zonder facet AgentIdentity,
    een app-only token (`idtyp=app`, `scp` leeg of `/`) en een token waarvan het onderwerp de
    agent zelf of zijn eigen gebruikersaccount is (`xms_sub_fct` met 11 of 13). De agent mag in
    deze opzet nooit zonder medewerker handelen.
    """
    if AGENT_IDENTITY not in _facetten(claims, "xms_act_fct"):
        raise ValueError("geen agenttoken: xms_act_fct bevat geen AgentIdentity (11)")
    if claims.get("idtyp") == "app":
        raise ValueError("app-only token: de agent handelt zelfstandig, niet namens een medewerker")
    if _facetten(claims, "xms_sub_fct") & {AGENT_IDENTITY, AGENT_GEBRUIKER}:
        raise ValueError("onderwerp is de agent zelf of zijn gebruikersaccount, geen medewerker")
    scopes = frozenset(str(claims.get("scp") or "").split()) - {"/"}
    if not scopes:
        raise ValueError("app-only token: geen gedelegeerde scopes, dus geen medewerker")
    agent = claims.get("azp") or claims.get("appid")
    if agent not in toegelaten_agents:
        raise ValueError(f"agent {agent} is niet toegelaten")
    medewerker = claims.get("oid")
    if not isinstance(medewerker, str) or not medewerker:
        raise ValueError("oid van de medewerker ontbreekt")
    return Delegatie(medewerker, str(agent), scopes)
