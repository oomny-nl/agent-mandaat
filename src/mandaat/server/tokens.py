"""Tokencontrole in de API: handtekening, audience, tokentype, introspectie en DPoP.

Alles faalt dicht: een onbereikbare Keycloak, een onleesbaar token of een ontbrekende claim leidt
tot weigering, nooit tot toelating.

Tokentype (RFC 8725 §3.11): Keycloak zet in de kop van elk token `typ: JWT`, ook bij ID-tokens; een
`at+jwt`-kop (RFC 9068) geeft het niet. Het onderscheid staat in Keycloaks eigen `typ`-claim:
`DPoP` voor een DPoP-gebonden access token, `Bearer` voor een gewoon access token, `ID` en
`Refresh` voor de andere soorten. De API controleert die claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx2
import jwt

from mandaat.config import Instellingen
from mandaat.dpop import DPoPFout, GevalideerdBewijs, valideer_bewijs

TOEGELATEN_AGENTS = frozenset({"dossierassistent"})


def actor(claims: dict[str, Any]) -> str | None:
    """`act.client_id` als het token een geldig gevormde actorclaim heeft, anders None."""
    act = claims.get("act")
    client_id = act.get("client_id") if isinstance(act, dict) else None
    return client_id if isinstance(client_id, str) else None


class TokenFout(Exception):
    """Token ongeldig; `code` is de OAuth-foutcode voor de challenge, `status` de HTTP-status."""

    def __init__(
        self,
        reden: str,
        code: str = "invalid_token",
        status: int = 401,
        claims: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(reden)
        self.reden = reden
        self.code = code
        self.status = status
        self.claims = claims or {}


@dataclass(frozen=True)
class Gecontroleerd:
    """Een gecontroleerd token met zijn claims."""

    claims: dict[str, Any]
    bewijs: GevalideerdBewijs | None = None

    @property
    def scopes(self) -> frozenset[str]:
        """Scopes uit het token."""
        return frozenset(str(self.claims.get("scope", "")).split())


class Tokencontrole:
    """Controleert tokens tegen de Keycloak-realm.

    Args:
        inst: Instellingen.
        clientgeheim: Geheim van de resource-client voor introspectie.
    """

    def __init__(self, inst: Instellingen, clientgeheim: str) -> None:
        self.inst = inst
        self._geheim = clientgeheim
        self._jwks = jwt.PyJWKClient(f"{inst.oidc}/certs", cache_jwk_set=True, lifespan=300)

    def _decodeer(self, token: str) -> dict[str, Any]:
        try:
            sleutel = self._jwks.get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                sleutel.key,
                algorithms=["RS256", "ES256"],
                audience=self.inst.resource_url,
                issuer=self.inst.issuer,
                leeway=30,
                options={"require": ["exp", "iat", "sub", "jti", "iss", "aud"]},
            )
        except jwt.PyJWKClientError as fout:
            raise TokenFout(f"sleutels van Keycloak niet op te halen: {fout}") from fout
        except jwt.PyJWTError as fout:
            raise TokenFout(f"token ongeldig: {fout}") from fout

    def _actief(self, token: str) -> None:
        try:
            antwoord = httpx2.post(
                f"{self.inst.oidc}/token/introspect",
                data={
                    "token": token,
                    "client_id": self.inst.resource_url,
                    "client_secret": self._geheim,
                },
                timeout=10,
            )
            inhoud = antwoord.json() if antwoord.status_code == 200 else None
            actief = isinstance(inhoud, dict) and inhoud.get("active") is True
        except (httpx2.HTTPError, ValueError) as fout:
            raise TokenFout(f"introspectie niet mogelijk, daarom geweigerd: {fout}") from fout
        if not actief:
            raise TokenFout("token is ingetrokken of niet meer actief (introspectie)")

    def actieve_sessies(self, sub: str) -> dict[str, frozenset[str]]:
        """Actieve Keycloak-sessies van een medewerker, via de Admin API.

        De resource-client heeft daarvoor alleen de rol `view-users`; schrijven kan hij niet.

        Returns:
            Per sessie (`sid`) de clientId's die in die sessie tokens kregen.

        Raises:
            TokenFout: Als Keycloak niet antwoordt (fail closed: dan geen nieuw mandaat).
        """
        try:
            antwoord = httpx2.post(
                f"{self.inst.oidc}/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.inst.resource_url,
                    "client_secret": self._geheim,
                },
                timeout=10,
            )
            token = antwoord.json()["access_token"]
            sessies = httpx2.get(
                f"{self.inst.keycloak_url}/admin/realms/{self.inst.realm}/users/{sub}/sessions",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            sessies.raise_for_status()
            return {
                str(s["id"]): frozenset(str(c) for c in (s.get("clients") or {}).values())
                for s in sessies.json()
            }
        except (httpx2.HTTPError, KeyError, TypeError, ValueError) as fout:
            raise TokenFout(
                f"sessies niet op te vragen, daarom geweigerd: {fout}", status=503
            ) from fout

    def agenttoken(self, autorisatie: str, dpop: str | None, htm: str, htu: str) -> Gecontroleerd:
        """Controleer een gedelegeerd agenttoken met DPoP-bewijs.

        Args:
            autorisatie: Waarde van de Authorization-header.
            dpop: Waarde van de DPoP-header.
            htm: HTTP-methode van het verzoek.
            htu: URI van het ontvangen verzoek (RFC 9449 §4.3 punt 9).

        Returns:
            Het gecontroleerde token.

        Raises:
            TokenFout: Bij elke afwijking.
        """
        schema, _, token = autorisatie.partition(" ")
        if schema != "DPoP" or not token:
            raise TokenFout("tools vereisen Authorization: DPoP met een DPoP-gebonden token")
        if not dpop:
            raise TokenFout("DPoP-bewijs ontbreekt", code="invalid_dpop_proof")
        claims = self._decodeer(token)
        if claims.get("typ") != "DPoP":
            raise TokenFout(
                f"verkeerd tokentype {claims.get('typ')!r}; tools vereisen een DPoP-gebonden "
                "access token",
                claims=claims,
            )
        if actor(claims) not in TOEGELATEN_AGENTS:
            raise TokenFout(
                "token zonder act van een toegelaten agent; tools zijn alleen voor agents",
                claims=claims,
            )
        cnf = claims.get("cnf")
        jkt = cnf.get("jkt") if isinstance(cnf, dict) else None
        if not isinstance(jkt, str) or not jkt:
            raise TokenFout("token is niet DPoP-gebonden (cnf.jkt ontbreekt)", claims=claims)
        try:
            bewijs = valideer_bewijs(dpop, htm, htu, token, jkt)
            self._actief(token)
        except DPoPFout as fout:
            raise TokenFout(fout.reden, code=fout.code, claims=claims) from fout
        except TokenFout as fout:
            raise TokenFout(fout.reden, fout.code, fout.status, claims) from fout
        return Gecontroleerd(claims, bewijs)

    def medewerkertoken(self, autorisatie: str, scope: str) -> Gecontroleerd:
        """Controleer een eigen token van de medewerker (beheer en eigen besluiten).

        Args:
            autorisatie: Waarde van de Authorization-header.
            scope: Vereiste scope.

        Returns:
            Het gecontroleerde token.

        Raises:
            TokenFout: Bij elke afwijking; ontbrekende scope geeft 403.
        """
        schema, _, token = autorisatie.partition(" ")
        if schema != "Bearer" or not token:
            raise TokenFout("Authorization: Bearer vereist")
        claims = self._decodeer(token)
        if claims.get("typ") != "Bearer":
            raise TokenFout(
                f"verkeerd tokentype {claims.get('typ')!r}; Bearer-access token vereist",
                claims=claims,
            )
        if "act" in claims or "may_act" in claims:
            raise TokenFout(
                "dit endpoint is alleen voor de medewerker zelf, niet voor een agent", claims=claims
            )
        if claims.get("azp") != self.inst.werkplek_id:
            raise TokenFout("token niet uitgegeven aan de werkplek", claims=claims)
        gecontroleerd = Gecontroleerd(claims)
        if scope not in gecontroleerd.scopes:
            raise TokenFout(
                f"scope {scope} vereist", code="insufficient_scope", status=403, claims=claims
            )
        try:
            self._actief(token)
        except TokenFout as fout:
            raise TokenFout(fout.reden, fout.code, fout.status, claims) from fout
        return gecontroleerd
