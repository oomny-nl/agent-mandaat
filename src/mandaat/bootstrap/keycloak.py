"""Richt de realm `lindevoort` in via de Keycloak Admin REST API.

Idempotent: bestaat de realm al, dan verandert er niets (een nieuwe realm zou nieuwe sleutels
krijgen terwijl de API de oude kent). `make demo` begint met `docker compose down -v`, dus elke
demo start vanuit dezelfde begintoestand. De FGAP v2-permissie en de client policy
met de `may-act` executor gaan via de API omdat ze niet betrouwbaar via een realm-import lopen.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Generator
from typing import Any

import httpx2

from mandaat.config import Instellingen
from mandaat.geheimen import Geheimen

TOOLSCOPES = ("dossier:lezen", "notitie:schrijven", "besluit:nemen")
AGENTSCOPES = ("dossier:lezen", "notitie:schrijven")
TOKEN_LEVENSDUUR_AGENT_S = 60

GEBRUIKERS = (
    {"username": "sanne", "firstName": "Sanne", "lastName": "de Vries", "rol": "klantmanager"},
    {"username": "bram", "firstName": "Bram", "lastName": "Jansen", "rol": "intake"},
)


class BootstrapFout(RuntimeError):
    """Een stap van de inrichting faalde."""


def _controleer(antwoord: httpx2.Response, stap: str) -> httpx2.Response:
    if antwoord.status_code >= 400:
        raise BootstrapFout(
            f"{stap}: HTTP {antwoord.status_code} {antwoord.text[:300]}. "
            "Controleer of Keycloak 26.8.1 draait met de features uit compose.yaml."
        )
    return antwoord


def wacht_op_keycloak(inst: Instellingen, timeout_s: int = 180) -> None:
    """Wacht tot de master-realm van Keycloak antwoordt."""
    einde = time.monotonic() + timeout_s
    while time.monotonic() < einde:
        try:
            if httpx2.get(f"{inst.keycloak_url}/realms/master", timeout=5).status_code == 200:
                return
        except httpx2.HTTPError:
            pass
        time.sleep(2)
    raise BootstrapFout(f"Keycloak op {inst.keycloak_url} niet bereikbaar na {timeout_s} s")


class _BeheerAuth(httpx2.Auth):
    """Vraagt een nieuw beheertoken aan zodra het vorige bijna verlopen is (60 s in master)."""

    def __init__(self, inst: Instellingen, gebruiker: str, wachtwoord: str) -> None:
        self._url = f"{inst.keycloak_url}/realms/master/protocol/openid-connect/token"
        self._gegevens = {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": gebruiker,
            "password": wachtwoord,
        }
        self._token: tuple[str, float] | None = None

    def _geldig_token(self) -> str:
        if self._token is None or self._token[1] < time.time() + 10:
            antwoord = _controleer(
                httpx2.post(self._url, data=self._gegevens, timeout=30), "aanmelden als beheerder"
            ).json()
            self._token = (antwoord["access_token"], time.time() + int(antwoord["expires_in"]))
        return self._token[0]

    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response]:
        """Zet een geldig beheertoken op het verzoek."""
        request.headers["Authorization"] = f"Bearer {self._geldig_token()}"
        yield request


def admin_client(inst: Instellingen, gebruiker: str, wachtwoord: str) -> httpx2.Client:
    """Client voor de Admin REST API, aangemeld via de master-realm (vernieuwt zelf)."""
    auth = _BeheerAuth(inst, gebruiker, wachtwoord)
    auth._geldig_token()  # meteen aanmelden, zodat een fout wachtwoord direct opvalt
    return httpx2.Client(base_url=f"{inst.keycloak_url}/admin/realms", auth=auth, timeout=30)


def _audience_mapper(doel: str) -> dict[str, Any]:
    return {
        "name": "audience",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "config": {
            "included.client.audience": doel,
            "access.token.claim": "true",
            "introspection.token.claim": "true",
        },
    }


def maak_realm(admin: httpx2.Client, inst: Instellingen) -> None:
    """Maak de realm aan, met events en FGAP v2."""
    _controleer(
        admin.post(
            "",
            json={
                "realm": inst.realm,
                "displayName": "Gemeente Lindevoort (fictief)",
                "enabled": True,
                "adminPermissionsEnabled": True,
                "eventsEnabled": True,
                "eventsExpiration": 86400,
                "adminEventsEnabled": True,
                "accessTokenLifespan": 300,
                "ssoSessionIdleTimeout": 1800,
                "internationalizationEnabled": True,
                "supportedLocales": ["nl", "en"],
                "defaultLocale": "nl",
            },
        ),
        "realm aanmaken",
    )


def maak_scopes(admin: httpx2.Client, inst: Instellingen) -> dict[str, str]:
    """Maak tool-, beheer- en taakscopes; geeft naam naar id."""
    base = f"/{inst.realm}/client-scopes"
    tekst = {
        "dossier:lezen": "Dossiers lezen",
        "notitie:schrijven": "Notities in dossiers schrijven",
        "besluit:nemen": "Besluiten vastleggen",
        "mandaat:beheren": "Mandaten voor de dossierassistent beheren",
    }
    for naam, uitleg in tekst.items():
        attributen = {"include.in.token.scope": "true", "display.on.consent.screen": "true"}
        attributen["consent.screen.text"] = uitleg
        body = {"name": naam, "protocol": "openid-connect", "attributes": attributen}
        body["protocolMappers"] = [_audience_mapper(inst.resource_url)]
        _controleer(admin.post(base, json=body), f"scope {naam}")
    taak = {
        "name": "taak",
        "protocol": "openid-connect",
        "attributes": {
            "include.in.token.scope": "true",
            "display.on.consent.screen": "true",
            "is.parameterized.scope": "true",
            "parameterized.scope.type": "custom",
            "parameterized.scope.regexp": r"^\d{4}-\d{4}$",
            "parameterized.scope.repeatable": "false",
        },
        "protocolMappers": [
            {
                "name": "taak",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-parameterized-scope-mapper",
                "config": {
                    "claim.name": "taak",
                    "access.token.claim": "true",
                    "introspection.token.claim": "true",
                    "id.token.claim": "false",
                    "jsonType.label": "String",
                    "multivalued": "false",
                },
            }
        ],
    }
    _controleer(admin.post(base, json=taak), "scope taak")
    alle = {s["name"]: s for s in _controleer(admin.get(base), "scopes lezen").json()}
    delegatie = alle["delegation:client"]
    delegatie.setdefault("attributes", {})["consent.screen.text"] = (
        "De dossierassistent namens u laten handelen"
    )
    _controleer(admin.put(f"{base}/{delegatie['id']}", json=delegatie), "consenttekst delegatie")
    return {naam: s["id"] for naam, s in alle.items()}


def _maak_client(admin: httpx2.Client, realm: str, body: dict[str, Any]) -> str:
    _controleer(admin.post(f"/{realm}/clients", json=body), f"client {body['clientId']}")
    clients = admin.get(f"/{realm}/clients", params={"clientId": body["clientId"]}).json()
    return clients[0]["id"]


def _koppel_scopes(admin: httpx2.Client, pad: str, scopes: dict[str, str], namen: tuple) -> None:
    for naam in namen:
        _controleer(admin.put(f"{pad}/optional-client-scopes/{scopes[naam]}"), f"scope {naam}")


def maak_clients(
    admin: httpx2.Client, inst: Instellingen, geheimen: Geheimen, scopes: dict[str, str]
) -> dict[str, str]:
    """Maak resource, werkplek en agent; geeft clientId naar intern id."""
    r = inst.realm
    ids = {}
    ids[inst.resource_url] = _maak_client(
        admin,
        r,
        {
            "clientId": inst.resource_url,
            "name": "Dossier-API (MCP-server)",
            "publicClient": False,
            "secret": geheimen.wachtwoord("resource-client"),
            "standardFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": False,
        },
    )
    ids[inst.werkplek_id] = _maak_client(
        admin,
        r,
        {
            "clientId": inst.werkplek_id,
            "name": "Werkplek klantmanagers",
            "publicClient": True,
            "consentRequired": True,
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False,
            "redirectUris": [inst.redirect_uri],
            "defaultClientScopes": ["basic", "profile"],
            "optionalClientScopes": [],
            "attributes": {"pkce.code.challenge.method": "S256", "post.logout.redirect.uris": "+"},
        },
    )
    ids[inst.agent_id] = _maak_client(
        admin,
        r,
        {
            "clientId": inst.agent_id,
            "name": "Dossierassistent (AI-agent)",
            "description": "Eigenaar: team Werk en Inkomen; sponsor: afdelingsmanager",
            "publicClient": False,
            "clientAuthenticatorType": "client-jwt",
            "serviceAccountsEnabled": True,
            "standardFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "fullScopeAllowed": False,
            "defaultClientScopes": ["basic"],
            "optionalClientScopes": [],
            "attributes": {
                "standard.token.exchange.enabled": "true",
                "access.token.lifespan": str(TOKEN_LEVENSDUUR_AGENT_S),
                "dpop.bound.access.tokens": "true",
                "use.jwks.string": "true",
                "jwks.string": json.dumps(geheimen.agent_jwks()),
                "token.endpoint.auth.signing.alg": "ES256",
            },
        },
    )
    werkplek_pad = f"/{r}/clients/{ids[inst.werkplek_id]}"
    _koppel_scopes(
        admin,
        werkplek_pad,
        scopes,
        ("taak", "mandaat:beheren", "besluit:nemen", "delegation:client"),
    )
    _koppel_scopes(admin, f"/{r}/clients/{ids[inst.agent_id]}", scopes, AGENTSCOPES)
    return ids


def maak_gebruikers(admin: httpx2.Client, inst: Instellingen, geheimen: Geheimen) -> None:
    """Maak de fictieve medewerkers met gegenereerde wachtwoorden."""
    for g in GEBRUIKERS:
        body = {
            "username": g["username"],
            "firstName": g["firstName"],
            "lastName": g["lastName"],
            "email": f"{g['username']}@lindevoort.example",
            "emailVerified": True,
            "enabled": True,
            "attributes": {"rol": [g["rol"]]},
            "credentials": [
                {
                    "type": "password",
                    "value": geheimen.wachtwoord(g["username"]),
                    "temporary": False,
                }
            ],
        }
        _controleer(admin.post(f"/{inst.realm}/users", json=body), f"gebruiker {g['username']}")


def maak_delegatiepermissie(admin: httpx2.Client, inst: Instellingen, ids: dict[str, str]) -> None:
    """FGAP v2: alleen de dossierassistent mag namens medewerkers handelen (scope `delegate`)."""
    r = inst.realm
    ap = admin.get(f"/{r}/clients", params={"clientId": "admin-permissions"}).json()[0]["id"]
    base = f"/{r}/clients/{ap}/authz/resource-server"
    beleid = _controleer(
        admin.post(
            f"{base}/policy/client",
            json={"name": "alleen-dossierassistent", "clients": [ids[inst.agent_id]]},
        ),
        "delegatiebeleid",
    ).json()
    _controleer(
        admin.post(
            f"{base}/permission/scope",
            json={
                "name": "dossierassistent-mag-namens-medewerkers-handelen",
                "resourceType": "Users",
                "scopes": ["delegate"],
                "policies": [beleid["id"]],
                "decisionStrategy": "UNANIMOUS",
            },
        ),
        "delegatiepermissie",
    )


def maak_clientpolicy(admin: httpx2.Client, inst: Instellingen) -> None:
    """Zet de `may-act` executor aan voor alle clients (hervalideert elke `may_act`-claim)."""
    r = inst.realm
    profiel = {
        "name": "delegatie-controle",
        "executors": [{"executor": "may-act", "configuration": {}}],
    }
    _controleer(
        admin.put(f"/{r}/client-policies/profiles", json={"profiles": [profiel]}),
        "client-policyprofiel",
    )
    beleid = {
        "name": "may-act-voor-alle-clients",
        "enabled": True,
        "conditions": [{"condition": "any-client", "configuration": {}}],
        "profiles": ["delegatie-controle"],
    }
    _controleer(
        admin.put(f"/{r}/client-policies/policies", json={"policies": [beleid]}),
        "client-policy",
    )


def richt_in(inst: Instellingen, geheimen: Geheimen, admin: httpx2.Client) -> None:
    """Voer alle inrichtingsstappen uit."""
    maak_realm(admin, inst)
    scopes = maak_scopes(admin, inst)
    ids = maak_clients(admin, inst, geheimen, scopes)
    maak_gebruikers(admin, inst, geheimen)
    maak_delegatiepermissie(admin, inst, ids)
    maak_clientpolicy(admin, inst)


def main() -> None:
    """Startpunt van de `init`-container."""
    inst = Instellingen.uit_omgeving()
    geheimen = Geheimen(inst.geheimen_dir)
    geheimen.zorg_dat_ze_bestaan()
    wacht_op_keycloak(inst)
    admin = admin_client(
        inst,
        os.environ.get("KC_BOOTSTRAP_ADMIN_USERNAME", "admin"),
        os.environ["KC_BOOTSTRAP_ADMIN_PASSWORD"],
    )
    if admin.get(f"/{inst.realm}").status_code == 200:
        print(f"Realm {inst.realm} bestaat al; inrichting overgeslagen")
        return
    try:
        richt_in(inst, geheimen, admin)
    except BootstrapFout as fout:
        print(f"Inrichting mislukt: {fout}", file=sys.stderr)
        raise SystemExit(1) from fout
    print(f"Realm {inst.realm} ingericht op {inst.keycloak_url}")


if __name__ == "__main__":
    main()
