"""Werkplek van de medewerker: inloggen, toestemming geven, mandaat registreren en intrekken.

De werkplek is in dit ontwerp een vertrouwde component (Sannes eigen client). Het brede
logintoken, het beheertoken en alle refresh tokens blijven hier; de agent krijgt alleen het
subject token, en dat heeft als audience uitsluitend de agent zelf.

Inloggen gebeurt headless: deze code vult het Keycloak-loginformulier in en klikt op het
toestemmingsscherm op "Ja", precies zoals een browser dat doet. Er wordt geen password grant
gebruikt; het is de gewone authorization code flow met PKCE en consent.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx2

from mandaat.config import Instellingen

FORM_ACTIE = re.compile(r'<form[^>]*action="([^"]+)"')
VERBORGEN = re.compile(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"')
TEKST = re.compile(r"<[^>]+>")


class WerkplekFout(RuntimeError):
    """Inloggen, toestemming of mandaatbeheer mislukte."""


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _formulier(pagina: str, basis: str) -> str:
    gevonden = FORM_ACTIE.search(pagina)
    if gevonden is None:
        raise WerkplekFout("Keycloak toonde geen formulier; controleer gebruiker en scopes")
    return urljoin(basis, html.unescape(gevonden.group(1)))


def zichtbare_tekst(pagina: str) -> str:
    """Leesbare tekst van een HTML-pagina (voor het tonen van het toestemmingsscherm)."""
    zonder_script = re.sub(r"<script.*?</script>", " ", pagina, flags=re.DOTALL)
    return re.sub(r"\s+", " ", html.unescape(TEKST.sub(" ", zonder_script))).strip()


@dataclass
class Tokens:
    """Tokens van één Keycloak-sessie van de medewerker."""

    access_token: str
    refresh_token: str
    id_token: str | None = None
    toestemmingsscherm: str = ""


@dataclass
class Werkplek:
    """Werkplek-client van één medewerker.

    Attributes:
        inst: Instellingen van de keten.
        gebruiker: Gebruikersnaam in Keycloak.
        wachtwoord: Wachtwoord van de gebruiker.
    """

    inst: Instellingen
    gebruiker: str
    wachtwoord: str
    sessie: Tokens | None = field(default=None, init=False)
    taak: str | None = field(default=None, init=False)

    def _browser(self) -> httpx2.Client:
        browser = httpx2.Client(follow_redirects=False, timeout=30)

        def onveilig_toestaan(_: httpx2.Response) -> None:
            # Keycloak zet Secure-cookies; in de lokale HTTP-keten moeten ze toch terug.
            for cookie in browser.cookies.jar:
                cookie.secure = False

        browser.event_hooks["response"] = [onveilig_toestaan]
        return browser

    def _naar_callback(self, browser: httpx2.Client, antwoord: httpx2.Response) -> httpx2.Response:
        while (
            antwoord.status_code in (302, 303) and "/callback" not in antwoord.headers["location"]
        ):
            antwoord = browser.get(urljoin(str(antwoord.url), antwoord.headers["location"]))
        return antwoord

    def login(self, taak: str, extra_scopes: tuple[str, ...] = ()) -> Tokens:
        """Log in met PKCE, geef toestemming voor de agent en de taak, en bewaar de sessie.

        Args:
            taak: Dossiernummer waarvoor de agent een mandaat krijgt, bijvoorbeeld `2026-0142`.
            extra_scopes: Extra scopes, bijvoorbeeld `besluit:nemen` voor een eigen besluit.

        Returns:
            De tokens van de nieuwe sessie (blijven in de werkplek).
        """
        self.log_uit()  # een nieuwe taak begint nooit naast een levende oude sessie
        scope = " ".join(
            (
                "openid",
                f"delegation:client:{self.inst.agent_id}",
                f"taak:{taak}",
                "mandaat:beheren",
                *extra_scopes,
            )
        )
        verifier, challenge = _pkce()
        browser = self._browser()
        antwoord = browser.get(
            f"{self.inst.oidc}/auth",
            params={
                "client_id": self.inst.werkplek_id,
                "response_type": "code",
                "redirect_uri": self.inst.redirect_uri,
                "scope": scope,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": secrets.token_urlsafe(12),
                "resource": self.inst.resource_url,
                "ui_locales": "nl",
            },
        )
        antwoord = browser.post(
            _formulier(antwoord.text, str(antwoord.url)),
            data={"username": self.gebruiker, "password": self.wachtwoord},
        )
        antwoord = self._naar_callback(browser, antwoord)
        scherm = ""
        if antwoord.status_code == 200:
            scherm = zichtbare_tekst(antwoord.text)
            verborgen = dict(VERBORGEN.findall(antwoord.text))
            antwoord = browser.post(
                _formulier(antwoord.text, str(antwoord.url)), data={**verborgen, "accept": "Ja"}
            )
        code = parse_qs(urlparse(antwoord.headers.get("location", "")).query).get("code", [None])[0]
        if code is None:
            raise WerkplekFout(f"geen autorisatiecode ontvangen (HTTP {antwoord.status_code})")
        tokens = self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.inst.redirect_uri,
                "code_verifier": verifier,
                "resource": self.inst.resource_url,
            }
        )
        self.sessie = Tokens(
            tokens["access_token"], tokens["refresh_token"], tokens.get("id_token"), scherm
        )
        self.taak = taak
        return self.sessie

    def _token(self, data: dict[str, str]) -> dict[str, Any]:
        antwoord = httpx2.post(
            f"{self.inst.oidc}/token", data={"client_id": self.inst.werkplek_id, **data}, timeout=30
        )
        if antwoord.status_code != 200:
            raise WerkplekFout(f"tokenverzoek geweigerd: {antwoord.text[:200]}")
        return antwoord.json()

    def _verfijn(self, scope: str) -> str:
        if self.sessie is None:
            raise WerkplekFout("niet ingelogd")
        tokens = self._token(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.sessie.refresh_token,
                "scope": scope,
                "resource": self.inst.resource_url,
            }
        )
        self.sessie.refresh_token = tokens.get("refresh_token", self.sessie.refresh_token)
        return tokens["access_token"]

    def beheertoken(self) -> str:
        """Token voor mandaatbeheer; verlaat de werkplek nooit."""
        return self._verfijn(f"openid mandaat:beheren taak:{self.taak}")

    def subject_token(self) -> str:
        """Token met `may_act` voor de agent; audience is alleen de agent zelf."""
        return self._verfijn(f"openid delegation:client:{self.inst.agent_id} taak:{self.taak}")

    def eigen_token(self, scope: str) -> str:
        """Token voor een eigen handeling van de medewerker, bijvoorbeeld een besluit."""
        return self._verfijn(f"openid {scope}")

    def _api(self) -> str:
        return self.inst.resource_url.rsplit("/", 1)[0]

    def registreer_mandaat(self) -> dict[str, Any]:
        """Registreer het mandaat in de API met het beheertoken."""
        antwoord = httpx2.post(
            f"{self._api()}/mandaten",
            headers={"Authorization": f"Bearer {self.beheertoken()}"},
            timeout=30,
        )
        if antwoord.status_code != 201:
            raise WerkplekFout(f"mandaat niet geregistreerd: {antwoord.text[:300]}")
        return antwoord.json()

    def trek_in(self, mandaat_id: str) -> None:
        """Trek het mandaat in en beëindig daarna de Keycloak-sessie (vaste volgorde)."""
        antwoord = httpx2.delete(
            f"{self._api()}/mandaten/{mandaat_id}",
            headers={"Authorization": f"Bearer {self.beheertoken()}"},
            timeout=30,
        )
        if antwoord.status_code != 200:
            raise WerkplekFout(f"intrekken mislukt: {antwoord.text[:300]}")
        self.log_uit()

    def log_uit(self) -> None:
        """Beëindig de Keycloak-sessie; het subject token van de agent wordt daarmee inactief."""
        if self.sessie is None:
            return
        antwoord = httpx2.post(
            f"{self.inst.oidc}/logout",
            data={"client_id": self.inst.werkplek_id, "refresh_token": self.sessie.refresh_token},
            timeout=30,
        )
        if antwoord.status_code not in (200, 204):
            raise WerkplekFout(f"uitloggen mislukt: HTTP {antwoord.status_code}")
        self.sessie = None
