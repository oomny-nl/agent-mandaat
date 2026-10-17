"""Levenscyclus van het mandaat: intrekken, vervangen, generatiebinding, noodstop, offboarding."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest

from mandaat.agent.makelaar import Makelaar, MandaatGeweigerd
from mandaat.config import Instellingen
from mandaat.server.opslag import controleer_tegen_anker, lees_anker
from mandaat.werkplek.sessie import WerkplekFout
from tests.integratie.conftest import roep, toolfout

pytestmark = pytest.mark.integratie


def test_vooraf_opgehaalde_tokens_na_intrekking(
    verleen: Any, makelaar: Makelaar, inst: Instellingen
) -> None:
    sessie = verleen()
    vooraf = [makelaar.gedelegeerd(sessie.subject_token, "dossier:lezen") for _ in range(2)]
    sessie.werkplek.trek_in(sessie.mandaat["id"])
    for token in vooraf:
        antwoord = roep(inst, token, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop)
        assert toolfout(antwoord) == "mandaat_ingetrokken"


def test_oud_subject_token_na_intrekking_geweigerd_door_keycloak(
    verleen: Any, makelaar: Makelaar
) -> None:
    sessie = verleen()
    sessie.werkplek.trek_in(sessie.mandaat["id"])
    with pytest.raises(MandaatGeweigerd) as fout:
        makelaar.gedelegeerd(sessie.subject_token, "dossier:lezen")
    assert fout.value.status == 400


def test_wisselproef(verleen: Any, makelaar: Makelaar, inst: Instellingen) -> None:
    """Token uit mandaat A (0142); A ingetrokken, B (0143) actief: token A blijft geweigerd."""
    a = verleen("sanne", "2026-0142")
    token_a = makelaar.gedelegeerd(a.subject_token, "dossier:lezen")
    a.werkplek.trek_in(a.mandaat["id"])
    time.sleep(1.1)  # zodat B in een latere seconde valt dan token A; dezelfde seconde: unittest
    verleen("sanne", "2026-0143")
    antwoord = roep(inst, token_a, "dossier_lezen", {"dossier": "2026-0143"}, makelaar.dpop)
    assert toolfout(antwoord) == "token_uit_eerder_mandaat"


def test_token_uit_dezelfde_seconde_als_registratie(
    verleen: Any, makelaar: Makelaar, inst: Instellingen
) -> None:
    sessie = verleen(wachten=False)
    token = makelaar.gedelegeerd(sessie.subject_token, "dossier:lezen")
    assert time.time() < sessie.mandaat["geldig_vanaf"], "test te traag voor deze proef"
    antwoord = roep(inst, token, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop)
    assert toolfout(antwoord) == "mandaat_nog_niet_geldig"


def test_tweede_mandaat_uit_dezelfde_sessie(verleen: Any) -> None:
    sessie = verleen()
    with pytest.raises(WerkplekFout, match="sessie"):
        sessie.werkplek.registreer_mandaat()


@pytest.fixture
def agentclient(admin: httpx2.Client, inst: Instellingen) -> Iterator[dict[str, Any]]:
    """De agentclient; wordt na de test altijd weer ingeschakeld."""
    pad = f"/{inst.realm}/clients"
    client = admin.get(pad, params={"clientId": inst.agent_id}).json()[0]
    yield client
    admin.put(f"{pad}/{client['id']}", json={**client, "enabled": True})


def test_noodstop(
    verleen: Any, makelaar: Makelaar, inst: Instellingen, admin: httpx2.Client, agentclient: Any
) -> None:
    sessie = verleen()
    lopend = makelaar.gedelegeerd(sessie.subject_token, "dossier:lezen")
    pad = f"/{inst.realm}/clients/{agentclient['id']}"
    assert admin.put(pad, json={**agentclient, "enabled": False}).status_code == 204
    antwoord = roep(inst, lopend, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop)
    assert antwoord.status_code == 401
    makelaar._actor = None
    with pytest.raises(MandaatGeweigerd) as fout:
        makelaar.gedelegeerd(sessie.subject_token, "dossier:lezen")
    assert fout.value.fout == "invalid_client"


def test_offboarding_medewerker(
    verleen: Any, makelaar: Makelaar, inst: Instellingen, admin: httpx2.Client
) -> None:
    sessie = verleen("bram", "2026-0142")
    lopend = makelaar.gedelegeerd(sessie.subject_token, "dossier:lezen")
    gebruiker = admin.get(
        f"/{inst.realm}/users", params={"username": "bram", "exact": "true"}
    ).json()[0]
    pad = f"/{inst.realm}/users/{gebruiker['id']}"
    admin.put(pad, json={**gebruiker, "enabled": False})
    try:
        antwoord = roep(inst, lopend, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop)
        assert antwoord.status_code == 401
    finally:
        admin.put(pad, json={**gebruiker, "enabled": True})


def test_auditketen_intact_en_compleet(inst: Instellingen) -> None:
    db = sqlite3.connect(f"file:{inst.db_pad}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    records = [
        {"nr": r["nr"], **json.loads(r["record"]), "vorige": r["vorige"], "hash": r["hash"]}
        for r in db.execute("SELECT * FROM audit ORDER BY nr")
    ]
    assert records, "geen auditrecords; draai deze test na de andere integratietests"
    assert controleer_tegen_anker(records, lees_anker(inst.anker_pad)) == (True, "intact")
    assert any(r.get("laag") == "api-beheer" and r["beslissing"] == "geweigerd" for r in records), (
        "geweigerde beheerverzoeken ontbreken in de audit"
    )
    for record in records:
        if record.get("laag") == "api-mandaat":
            velden = ("medewerker", "agent", "tool", "scope", "dossier", "token_jti", "mandaat")
            for veld in velden:
                assert record.get(veld) is not None, (veld, record)


def test_keycloak_logt_geweigerde_exchanges(admin: httpx2.Client, inst: Instellingen) -> None:
    events = admin.get(
        f"/{inst.realm}/events", params={"type": "TOKEN_EXCHANGE_ERROR", "max": 100}
    ).json()
    besluit = [e for e in events if "besluit:nemen" in str(e.get("details", {}).get("reason"))]
    assert besluit, "geweigerde besluit-exchange ontbreekt in de Keycloak-events"
    details = besluit[0]["details"]
    assert details["actor"] == inst.agent_id
    assert details["subject_token_client_id"] == inst.werkplek_id
