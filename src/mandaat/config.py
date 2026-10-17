"""Instellingen uit de omgeving, gedeeld door API, agent, werkplek en bootstrap."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(naam: str, standaard: str) -> str:
    return os.environ.get(naam, standaard)


@dataclass(frozen=True)
class Instellingen:
    """Configuratie van de demo-keten.

    Attributes:
        keycloak_url: Basis-URL van Keycloak zoals de containers hem bereiken.
        realm: Naam van de realm.
        resource_url: Canonieke URI van de MCP-server; ook de clientId van de resource in Keycloak.
        geheimen_dir: Map met lokaal gegenereerde sleutels en wachtwoorden (nooit in git).
        db_pad: SQLite-bestand van de API (mandaten, eenmalig gebruik, audit).
        anker_pad: Apart bestand (eigen volume) met de ketenkop van het auditlog per record.
        max_skew: Maximale klokafwijking in seconden tussen Keycloak en API.
        agent_id: clientId van de agent.
        werkplek_id: clientId van de werkplek van de medewerker.
        redirect_uri: Redirect-URI van de werkplek.
    """

    keycloak_url: str
    realm: str
    resource_url: str
    geheimen_dir: Path
    db_pad: Path
    max_skew: int
    anker_pad: Path = Path("/anker/audit-anker.log")
    agent_id: str = "dossierassistent"
    werkplek_id: str = "werkplek"
    redirect_uri: str = "http://localhost:8765/callback"

    @property
    def issuer(self) -> str:
        """Issuer van de realm (Keycloak bepaalt hem uit de host waarmee hij bereikt wordt)."""
        return f"{self.keycloak_url}/realms/{self.realm}"

    @property
    def oidc(self) -> str:
        """Basis van de OpenID Connect-endpoints van de realm."""
        return f"{self.issuer}/protocol/openid-connect"

    @classmethod
    def uit_omgeving(cls) -> Instellingen:
        """Lees de instellingen uit de omgeving; standaardwaarden horen bij de compose-keten."""
        return cls(
            keycloak_url=_env("KEYCLOAK_URL", "http://keycloak:8080").rstrip("/"),
            realm=_env("REALM", "lindevoort"),
            resource_url=_env("RESOURCE_URL", "http://dossier-mcp:8000/mcp"),
            geheimen_dir=Path(_env("GEHEIMEN_DIR", "/geheimen")),
            db_pad=Path(_env("DB_PAD", "/data/mandaat.db")),
            max_skew=int(_env("MAX_SKEW", "2")),
            anker_pad=Path(_env("ANKER_PAD", "/anker/audit-anker.log")),
        )
