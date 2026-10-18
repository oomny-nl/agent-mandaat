"""Tokenmakelaar van de agent: eigen identiteit plus per actie een gedelegeerd token.

1. Discovery: de agent leest de Protected Resource Metadata van de MCP-server (RFC 9728) en daaruit
   de metadata van de autorisatieserver (RFC 8414). Geen hardcoded issuer.
2. Eigen identiteit: `client_credentials` met `private_key_jwt` (geen gedeeld geheim) en DPoP. Dat
   geeft het actortoken, gebonden aan de DPoP-sleutel van de agent.
3. Per toolactie: token exchange (RFC 8693) met het subject token van de medewerker en het eigen
   actortoken. Alleen de scope voor die tool, 60 seconden, eenmalig bruikbaar bij de API.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx2
import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from mandaat.dpop import DPoPSleutel
from mandaat.geheimen import AGENT_KID

TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"  # noqa: S105 - URN
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105 - URN
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


class MandaatGeweigerd(Exception):
    """Keycloak weigert een token voor de agent; `fout` is de OAuth-foutcode."""

    def __init__(self, fout: str, beschrijving: str, status: int) -> None:
        super().__init__(f"{fout}: {beschrijving}")
        self.fout = fout
        self.beschrijving = beschrijving
        self.status = status


@dataclass(frozen=True)
class Metadata:
    """Uit discovery afgeleide endpoints."""

    resource: str
    issuer: str
    token_endpoint: str
    scopes: tuple[str, ...]


def well_known(url: str, naam: str) -> str:
    """Well-known-URI bij een URL met pad: ingevoegd tussen host en pad (RFC 8414 §3.1, RFC 9728).

    `http://keycloak:8080/realms/lindevoort` wordt
    `http://keycloak:8080/.well-known/oauth-authorization-server/realms/lindevoort`.
    """
    deel = urlsplit(url)
    return urlunsplit(
        (deel.scheme, deel.netloc, f"/.well-known/{naam}{deel.path.rstrip('/')}", "", "")
    )


def ontdek(resource_url: str) -> Metadata:
    """Ontdek de autorisatieserver via de MCP-server (RFC 9728, daarna RFC 8414).

    Args:
        resource_url: Canonieke URI van de MCP-server.

    Returns:
        Resource, issuer, token-endpoint en ondersteunde scopes.

    Raises:
        ValueError: Als `resource` of `issuer` in de metadata niet klopt (RFC 9728 §3.3,
            RFC 8414 §3.3).
    """
    prm_url = well_known(resource_url, "oauth-protected-resource")
    prm = httpx2.get(prm_url, timeout=10).raise_for_status().json()
    if prm["resource"] != resource_url:
        raise ValueError(f"metadata hoort bij {prm['resource']}, niet bij {resource_url}")
    issuer = prm["authorization_servers"][0]
    as_url = well_known(issuer, "oauth-authorization-server")
    meta = httpx2.get(as_url, timeout=10).raise_for_status().json()
    if meta["issuer"] != issuer:
        raise ValueError(f"issuer {meta['issuer']} wijkt af van {issuer}")
    return Metadata(
        resource=resource_url,
        issuer=issuer,
        token_endpoint=meta["token_endpoint"],
        scopes=tuple(prm.get("scopes_supported", ())),
    )


@dataclass(frozen=True)
class GedelegeerdToken:
    """Een gedelegeerd, DPoP-gebonden token voor één toolactie."""

    waarde: str
    scope: str
    jti: str
    verloopt: int


@dataclass
class Makelaar:
    """Haalt tokens op namens de agent.

    Attributes:
        agent_id: clientId van de agent.
        clientsleutel: Sleutel voor `private_key_jwt`.
        dpop: DPoP-sleutel van de agent.
        meta: Resultaat van discovery.
    """

    agent_id: str
    clientsleutel: ec.EllipticCurvePrivateKey
    dpop: DPoPSleutel
    meta: Metadata
    _actor: tuple[str, float] | None = field(default=None, init=False)

    def _assertion(self) -> str:
        nu = int(time.time())
        claims = {
            "iss": self.agent_id,
            "sub": self.agent_id,
            "aud": self.meta.issuer,
            "jti": str(uuid.uuid4()),
            "iat": nu,
            "exp": nu + 60,
        }
        return jwt.encode(claims, self.clientsleutel, algorithm="ES256", headers={"kid": AGENT_KID})

    def _post(self, data: dict[str, str]) -> dict[str, Any]:
        url = self.meta.token_endpoint
        antwoord = httpx2.post(
            url,
            data={
                "client_id": self.agent_id,
                "client_assertion_type": ASSERTION_TYPE,
                "client_assertion": self._assertion(),
                **data,
            },
            headers={"DPoP": self.dpop.bewijs("POST", url)},
            timeout=30,
        )
        if antwoord.status_code != 200:
            fout = antwoord.json() if antwoord.content else {}
            raise MandaatGeweigerd(
                fout.get("error", "onbekend"),
                fout.get("error_description", antwoord.text[:200]),
                antwoord.status_code,
            )
        return antwoord.json()

    def actortoken(self) -> str:
        """Eigen token van de agent (client_credentials), gecachet tot kort voor verloop."""
        if self._actor and self._actor[1] > time.time() + 10:
            return self._actor[0]
        antwoord = self._post({"grant_type": "client_credentials"})
        self._actor = (antwoord["access_token"], time.time() + int(antwoord["expires_in"]))
        return self._actor[0]

    def gedelegeerd(self, subject_token: str, scope: str) -> GedelegeerdToken:
        """Wissel het subject token van de medewerker in voor een token voor één toolactie.

        Args:
            subject_token: Token van de medewerker met `may_act` voor deze agent.
            scope: De ene scope die de tool nodig heeft.

        Returns:
            Het gedelegeerde token.

        Raises:
            MandaatGeweigerd: Als Keycloak de exchange weigert (bijvoorbeeld `invalid_scope`).
        """
        antwoord = self._post(
            {
                "grant_type": TOKEN_EXCHANGE,
                "subject_token": subject_token,
                "subject_token_type": ACCESS_TOKEN_TYPE,
                "actor_token": self.actortoken(),
                "actor_token_type": ACCESS_TOKEN_TYPE,
                "requested_token_type": ACCESS_TOKEN_TYPE,
                "scope": scope,
                "audience": self.meta.resource,
                "resource": self.meta.resource,
            }
        )
        waarde = antwoord["access_token"]
        claims = jwt.decode(waarde, options={"verify_signature": False})
        return GedelegeerdToken(waarde, antwoord.get("scope", scope), claims["jti"], claims["exp"])
