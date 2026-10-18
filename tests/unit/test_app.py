"""De ASGI-app zonder Keycloak: transportbeveiliging, eigen besluit en gedeelde onderdelen."""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import pytest
from starlette.testclient import TestClient

from mandaat.config import Instellingen
from mandaat.dpop import GevalideerdBewijs
from mandaat.server.app import MAX_UITKOMST, Dienst, maak_app
from mandaat.server.opslag import Opslag
from mandaat.server.tokens import Gecontroleerd

BASIS = "http://dossier-mcp:8000"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_KOPPEN = {"accept": "application/json, text/event-stream"}


class _Controle:
    """Laat elk token door; de tokencontrole zelf staat in test_tokens.py."""

    def agenttoken(self, *_a: Any) -> Gecontroleerd:
        claims = {"sub": "s", "scope": "dossier:lezen", "act": {"client_id": "dossierassistent"}}
        bewijs = GevalideerdBewijs("jkt", str(uuid.uuid4()), int(time.time()))
        return Gecontroleerd(claims, bewijs)

    def medewerkertoken(self, *_a: Any) -> Gecontroleerd:
        return Gecontroleerd({"sub": "sub-sanne", "preferred_username": "sanne"})


def _inst(tmp_path: Path) -> Instellingen:
    return Instellingen(
        keycloak_url="http://keycloak:8080",
        realm="lindevoort",
        resource_url=f"{BASIS}/mcp",
        geheimen_dir=tmp_path / "geheimen",
        db_pad=tmp_path / "db" / "mandaat.db",
        max_skew=2,
        anker_pad=tmp_path / "anker" / "anker.log",
    )


@pytest.fixture
def dienst(tmp_path: Path) -> Dienst:
    d = Dienst(_inst(tmp_path))
    d._controle = cast(Any, _Controle())
    return d


@pytest.fixture
def client(dienst: Dienst) -> Any:
    with TestClient(maak_app(dienst), base_url=BASIS) as c:
        yield c


def test_mcp_via_de_canonieke_host(client: TestClient) -> None:
    antwoord = client.post("/mcp", json=INITIALIZE, headers=MCP_KOPPEN)
    assert antwoord.status_code == 200
    assert antwoord.json()["result"]["serverInfo"]["name"] == "dossier-lindevoort"


def test_andere_host_geweigerd(client: TestClient) -> None:
    koppen = {**MCP_KOPPEN, "host": "aanvaller.example:8000"}
    assert client.post("/mcp", json=INITIALIZE, headers=koppen).status_code == 421


def test_origin_van_een_browser_geweigerd(client: TestClient) -> None:
    koppen = {**MCP_KOPPEN, "origin": "http://aanvaller.example"}
    assert client.post("/mcp", json=INITIALIZE, headers=koppen).status_code == 403


@pytest.mark.parametrize(
    ("body", "melding"),
    [
        (b"{", "geen geldige JSON"),
        (b"[]", "uitkomst"),
        (b"{}", "uitkomst"),
        (b'{"uitkomst": {"genest": "waarde"}}', "uitkomst"),
        (b'{"uitkomst": 1}', "uitkomst"),
        (b'{"uitkomst": "   "}', "uitkomst"),
        (('{"uitkomst": "' + "x" * (MAX_UITKOMST + 1) + '"}').encode(), "langer dan"),
        (b'{"uitkomst": "' + b"x" * 70000 + b'"}', "groter dan"),
    ],
)
def test_ongeldig_besluitverzoek_geeft_400_met_audit(
    client: TestClient, dienst: Dienst, body: bytes, melding: str
) -> None:
    antwoord = client.post("/dossiers/2026-0142/besluit", content=body)
    assert antwoord.status_code == 400
    assert melding in antwoord.json()["error_description"]
    record = dienst.opslag.auditlog()[-1]
    assert (record["beslissing"], record["laag"]) == ("geweigerd", "api-medewerker")
    assert record["reden"].startswith("ongeldig verzoek")


def test_geldig_eigen_besluit(client: TestClient, dienst: Dienst) -> None:
    antwoord = client.post("/dossiers/2026-0142/besluit", json={"uitkomst": " toegekend "})
    assert antwoord.status_code == 200
    assert antwoord.json()["uitkomst"] == "toegekend"
    assert dienst.opslag.auditlog()[-1]["beslissing"] == "toegestaan"


def test_een_opslag_bij_gelijktijdige_eerste_verzoeken(tmp_path: Path) -> None:
    d = Dienst(_inst(tmp_path))
    start = threading.Barrier(16)

    def pak(_: int) -> Opslag:
        start.wait()
        return d.opslag

    with ThreadPoolExecutor(16) as pool:
        instanties = {id(o) for o in pool.map(pak, range(16))}
    assert len(instanties) == 1


class _Registratie(_Controle):
    """Medewerkertoken uit sessie `eigen` met taak 2026-0142; sessies naar keuze van de test."""

    def __init__(self, sessies: dict[str, frozenset[str]]) -> None:
        self.sessies = sessies

    def medewerkertoken(self, *_a: Any) -> Gecontroleerd:
        claims = {"sub": "sub-sanne", "preferred_username": "sanne", "taak": "2026-0142"}
        return Gecontroleerd({**claims, "sid": "eigen"})

    def actieve_sessies(self, _sub: str) -> dict[str, frozenset[str]]:
        return self.sessies


@pytest.mark.parametrize(
    ("sessies", "status"),
    [
        ({"eigen": frozenset({"werkplek"})}, 201),
        ({"eigen": frozenset({"werkplek"}), "elders": frozenset({"account-console"})}, 201),
        ({"eigen": frozenset({"werkplek"}), "parallel": frozenset({"werkplek"})}, 409),
    ],
)
def test_registratie_alleen_zonder_andere_werkpleksessie(
    dienst: Dienst, sessies: dict[str, frozenset[str]], status: int
) -> None:
    dienst._controle = cast(Any, _Registratie(sessies))
    with TestClient(maak_app(dienst), base_url=BASIS) as c:
        antwoord = c.post("/mandaten")
    assert antwoord.status_code == status, antwoord.text
    if status == 409:
        assert "andere werkpleksessie" in antwoord.json()["error"]
        assert dienst.opslag.auditlog()[-1]["beslissing"] == "geweigerd"
