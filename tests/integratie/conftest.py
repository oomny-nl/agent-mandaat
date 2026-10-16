"""Fixtures voor integratietests tegen de echte compose-keten (Keycloak en dossier-mcp)."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest

from mandaat.agent.makelaar import GedelegeerdToken, Makelaar, ontdek
from mandaat.bootstrap.keycloak import admin_client
from mandaat.config import Instellingen
from mandaat.dpop import DPoPSleutel
from mandaat.geheimen import Geheimen
from mandaat.werkplek.sessie import Werkplek

MCP_KOPPEN = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}


@pytest.fixture(scope="session")
def inst() -> Instellingen:
    return Instellingen.uit_omgeving()


@pytest.fixture(scope="session")
def geheimen(inst: Instellingen) -> Geheimen:
    return Geheimen(inst.geheimen_dir)


@pytest.fixture(scope="session")
def admin(inst: Instellingen) -> httpx2.Client:
    return admin_client(inst, "admin", os.environ["KC_BOOTSTRAP_ADMIN_PASSWORD"])


@pytest.fixture(scope="session")
def makelaar(inst: Instellingen, geheimen: Geheimen) -> Makelaar:
    return Makelaar(
        inst.agent_id,
        geheimen.agent_clientsleutel(),
        geheimen.agent_dpop(),
        ontdek(inst.resource_url),
    )


@dataclass
class Sessie:
    """Een ingelogde medewerker met een actief mandaat."""

    werkplek: Werkplek
    mandaat: dict[str, Any]
    subject_token: str


@pytest.fixture
def verleen(inst: Instellingen, geheimen: Geheimen) -> Iterator[Any]:
    """Fabriek: log in, registreer een mandaat en wacht tot het geldig is. Ruimt sessies op."""
    sessies: list[Werkplek] = []

    def maak(gebruiker: str = "sanne", taak: str = "2026-0142", wachten: bool = True) -> Sessie:
        werkplek = Werkplek(inst, gebruiker, geheimen.wachtwoord(gebruiker))
        werkplek.login(taak)
        mandaat = werkplek.registreer_mandaat()
        if wachten:
            time.sleep(max(0.0, mandaat["geldig_vanaf"] - time.time() + 0.1))
        sessies.append(werkplek)
        return Sessie(werkplek, mandaat, werkplek.subject_token())

    yield maak
    for werkplek in sessies:
        werkplek.log_uit()


def roep(
    inst: Instellingen,
    token: GedelegeerdToken | str,
    tool: str,
    argumenten: dict[str, Any],
    sleutel: DPoPSleutel,
    **opties: Any,
) -> httpx2.Response:
    """Ruwe JSON-RPC `tools/call` met zelfgekozen token, schema en DPoP-bewijs."""
    waarde = token.waarde if isinstance(token, GedelegeerdToken) else token
    bewijs = opties.get("bewijs") or sleutel.bewijs(
        opties.get("htm", "POST"), opties.get("htu", inst.resource_url), access_token=waarde
    )
    koppen = {
        **MCP_KOPPEN,
        "Authorization": f"{opties.get('schema', 'DPoP')} {waarde}",
        "DPoP": bewijs,
    }
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": argumenten},
    }
    return httpx2.post(inst.resource_url, json=body, headers=koppen, timeout=30)


def toolfout(antwoord: httpx2.Response) -> str | None:
    """Reden van een toolweigering, of None als de tool slaagde."""
    assert antwoord.status_code == 200, antwoord.text
    resultaat = antwoord.json()["result"]
    if not resultaat.get("isError"):
        return None
    return resultaat["content"][0]["text"].rsplit(": ", 1)[-1]
