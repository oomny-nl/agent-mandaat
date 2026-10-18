"""Mandaatregister, binding aan de uitgiftetijd, eenmalig gebruik en auditketen."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from mandaat.server.opslag import (
    HistorieGewijzigd,
    Opslag,
    controleer_keten,
    controleer_tegen_anker,
    lees_anker,
)

ISS = "http://keycloak:8080/realms/lindevoort"


@pytest.fixture
def opslag(tmp_path: Path) -> Opslag:
    return Opslag(tmp_path / "mandaat.db", max_skew=2)


def mandaat(opslag: Opslag, dossier: str = "2026-0142", sid: str | None = None) -> dict[str, Any]:
    gecontroleerd = opslag.eerdere_sessies("sub-sanne", "dossierassistent")
    return opslag.registreer_mandaat(
        {
            "medewerker": "sub-sanne",
            "gebruiker": "sanne",
            "naam": "Sanne de Vries",
            "agent": "dossierassistent",
            "dossier": dossier,
            "sid": sid or str(uuid.uuid4()),
        },
        gecontroleerd,
    )


def claims(iat: float, jti: str | None = None) -> dict[str, Any]:
    return {
        "iss": ISS,
        "sub": "sub-sanne",
        "act": {"client_id": "dossierassistent"},
        "iat": int(iat),
        "exp": int(iat) + 60,
        "jti": jti or str(uuid.uuid4()),
    }


def test_geldig_vanaf_ligt_strikt_na_registratie(opslag: Opslag) -> None:
    m = mandaat(opslag)
    assert m["geldig_vanaf"] == math.floor(m["verleend_om"]) + 1 + 2


def test_toegestaan_binnen_mandaat(opslag: Opslag) -> None:
    m = mandaat(opslag)
    besluit = opslag.laat_toe(claims(m["geldig_vanaf"]), "2026-0142")
    assert (besluit.toegestaan, besluit.reden) == (True, "binnen_mandaat")


def test_token_uit_dezelfde_seconde_als_registratie(opslag: Opslag) -> None:
    m = mandaat(opslag)
    besluit = opslag.laat_toe(claims(math.floor(m["verleend_om"])), "2026-0142")
    assert besluit.reden == "mandaat_nog_niet_geldig"


def test_dossier_buiten_mandaat(opslag: Opslag) -> None:
    m = mandaat(opslag)
    assert opslag.laat_toe(claims(m["geldig_vanaf"]), "2026-0157").reden == "dossier_buiten_mandaat"


def test_token_eenmalig(opslag: Opslag) -> None:
    m = mandaat(opslag)
    c = claims(m["geldig_vanaf"])
    assert opslag.laat_toe(c, "2026-0142").toegestaan
    assert opslag.laat_toe(c, "2026-0142").reden == "token_al_gebruikt"


def test_geweigerd_token_wordt_niet_verbruikt(opslag: Opslag) -> None:
    m = mandaat(opslag)
    c = claims(m["geldig_vanaf"])
    assert not opslag.laat_toe(c, "2026-0157").toegestaan
    assert opslag.laat_toe(c, "2026-0142").toegestaan


def test_intrekking_werkt_voor_al_uitgegeven_tokens(opslag: Opslag) -> None:
    m = mandaat(opslag)
    vooraf = [claims(m["geldig_vanaf"]) for _ in range(2)]
    assert opslag.trek_in(m["id"], "sub-sanne")
    assert [opslag.laat_toe(c, "2026-0142").reden for c in vooraf] == ["mandaat_ingetrokken"] * 2


def test_alleen_eigenaar_trekt_in(opslag: Opslag) -> None:
    m = mandaat(opslag)
    assert not opslag.trek_in(m["id"], "sub-iemand-anders")


def test_wisselproef_oud_token_krijgt_geen_nieuw_mandaat(opslag: Opslag) -> None:
    a = mandaat(opslag, "2026-0142")
    token_a = claims(a["geldig_vanaf"])
    opslag.trek_in(a["id"], "sub-sanne")
    time.sleep(max(0.0, a["geldig_vanaf"] - time.time()) + 1.1)
    mandaat(opslag, "2026-0157")
    besluit = opslag.laat_toe(token_a, "2026-0157")
    assert (besluit.toegestaan, besluit.reden) == (False, "token_uit_eerder_mandaat")


def test_geen_tweede_actief_mandaat(opslag: Opslag) -> None:
    """Vervangen gaat alleen via intrekken; zo kan de werkplek de oude sessie eerst afsluiten."""
    a = mandaat(opslag)
    with pytest.raises(ValueError, match="actief mandaat"):
        mandaat(opslag, "2026-0143")
    opslag.trek_in(a["id"], "sub-sanne")
    assert mandaat(opslag, "2026-0143")["status"] == "actief"


def test_eerdere_sessies(opslag: Opslag) -> None:
    a = mandaat(opslag, sid="sessie-a")
    opslag.trek_in(a["id"], "sub-sanne")
    mandaat(opslag, "2026-0143", sid="sessie-b")
    assert opslag.eerdere_sessies("sub-sanne", "dossierassistent") == {"sessie-a", "sessie-b"}


def test_een_mandaat_per_sessie(opslag: Opslag) -> None:
    mandaat(opslag, sid="sessie-1")
    with pytest.raises(ValueError, match="sessie"):
        mandaat(opslag, "2026-0143", sid="sessie-1")


def test_geen_mandaat(opslag: Opslag) -> None:
    assert opslag.laat_toe(claims(time.time()), "2026-0142").reden == "geen_mandaat"


def test_gelijktijdig_gebruik_van_een_token(opslag: Opslag) -> None:
    m = mandaat(opslag)
    c = claims(m["geldig_vanaf"])
    start = threading.Barrier(16)

    def poging(_: int) -> str:
        start.wait()
        return opslag.laat_toe(c, "2026-0142").reden

    # map() geeft elke uitzondering in een draad door; zo telt een gecrashte poging niet stil mee.
    with ThreadPoolExecutor(16) as pool:
        redenen = list(pool.map(poging, range(16)))
    assert sorted(redenen) == ["binnen_mandaat"] + ["token_al_gebruikt"] * 15


def test_registratie_en_intrekking_schrijven_hun_audit_mee(tmp_path: Path) -> None:
    opslag = _met_anker(tmp_path)
    gegevens = {
        "medewerker": "sub-sanne",
        "gebruiker": "sanne",
        "naam": "",
        "agent": "dossierassistent",
        "dossier": "2026-0142",
        "sid": "sessie-1",
    }
    m = opslag.registreer_mandaat(gegevens, set(), audit={"beslissing": "mandaat_verleend"})
    assert opslag.trek_in(m["id"], "sub-sanne", audit={"beslissing": "mandaat_ingetrokken"})
    assert not opslag.trek_in(m["id"], "sub-sanne", audit={"beslissing": "mandaat_ingetrokken"})
    records = opslag.auditlog()
    assert [(r["beslissing"], r["mandaat"]) for r in records] == [
        ("mandaat_verleend", m["id"]),
        ("mandaat_ingetrokken", m["id"]),
    ]
    assert controleer_tegen_anker(records, lees_anker(tmp_path / "anker" / "anker.log")) == (
        True,
        "intact",
    )


def test_mislukt_auditrecord_draait_de_registratie_terug(opslag: Opslag) -> None:
    gegevens = {
        "medewerker": "sub-sanne",
        "gebruiker": "sanne",
        "naam": "",
        "agent": "dossierassistent",
        "dossier": "2026-0142",
        "sid": "sessie-1",
    }
    with pytest.raises(TypeError):
        opslag.registreer_mandaat(gegevens, set(), audit={"niet-serialiseerbaar": object()})
    assert opslag.mandaten() == []
    assert opslag.auditlog() == []


def test_notitie_en_besluit_schrijven_hun_audit_mee(opslag: Opslag) -> None:
    nr = opslag.voeg_notitie_toe("2026-0142", "t", "sanne", "agent", audit={"beslissing": "ja"})
    besluit = opslag.leg_besluit_vast("2026-0142", "toegekend", "sanne", audit={"beslissing": "ja"})
    assert [(r.get("notitie"), r.get("besluit")) for r in opslag.auditlog()] == [
        (nr, None),
        (None, besluit),
    ]


def test_mislukte_wijziging_laat_geen_toegestaan_achter(opslag: Opslag) -> None:
    opslag._db.execute("DROP TABLE besluiten")
    with pytest.raises(sqlite3.OperationalError):
        opslag.leg_besluit_vast("2026-0142", "toegekend", "sanne", audit={"beslissing": "ja"})
    assert opslag.auditlog() == []


def test_mislukt_auditrecord_draait_de_notitie_terug(opslag: Opslag) -> None:
    with pytest.raises(TypeError):
        opslag.voeg_notitie_toe("2026-0142", "t", "sanne", "agent", audit={"x": object()})
    assert opslag.notities("2026-0142") == []
    assert opslag.auditlog() == []


def test_auditketen_blijft_heel_bij_twee_verbindingen(tmp_path: Path) -> None:
    """Twee processen op één database: BEGIN IMMEDIATE voorkomt een vertakte keten.

    Beide schrijven naar hetzelfde anker. De volgorde van de ankerregels kan dan afwijken van die
    van de records; keten en anker moeten toch intact zijn.
    """
    anker = tmp_path / "anker" / "anker.log"
    a, b = (Opslag(tmp_path / "gedeeld.db", 2, anker) for _ in range(2))
    start = threading.Barrier(8)

    def schrijf(i: int) -> None:
        start.wait()
        for j in range(25):
            (a if i % 2 else b).audit({"beslissing": "toegestaan", "i": i, "j": j})

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(schrijf, range(8)))
    records = a.auditlog()
    regels = lees_anker(anker)
    assert len(records) == 200
    assert regels is not None
    assert sorted(nr for nr, _ in regels) == list(range(1, 201))
    assert controleer_tegen_anker(records, regels) == (True, "intact")


def test_anker_in_afwijkende_volgorde_is_intact(tmp_path: Path) -> None:
    opslag = _met_anker(tmp_path)
    for i in range(2):
        opslag.audit({"beslissing": "toegestaan", "i": i})
    records = opslag.auditlog()
    omgedraaid = [(r["nr"], r["hash"]) for r in reversed(records)]
    assert controleer_tegen_anker(records, omgedraaid) == (True, "intact")
    assert controleer_tegen_anker(records[:1], omgedraaid)[0] is False


def test_mislukte_commit_laat_de_verbinding_bruikbaar(tmp_path: Path) -> None:
    """Een lezer met een open transactie blokkeert COMMIT; daarna moet de opslag gewoon werken."""
    pad = tmp_path / "m.db"
    opslag = Opslag(pad, 2)
    opslag._db.execute("PRAGMA busy_timeout = 100")
    lezer = sqlite3.connect(pad, isolation_level=None)
    lezer.execute("BEGIN")
    lezer.execute("SELECT * FROM audit").fetchall()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        opslag.audit({"beslissing": "toegestaan", "poging": 1})
    assert not opslag._db.in_transaction
    lezer.execute("ROLLBACK")
    lezer.close()
    record = opslag.audit({"beslissing": "toegestaan", "poging": 2})
    assert [r["poging"] for r in opslag.auditlog()] == [2]
    assert record["nr"] == 1


def test_dpop_bewijs_eenmalig(opslag: Opslag) -> None:
    tot = int(time.time()) + 60
    assert opslag.dpop_bewijs_eenmalig("jkt", "jti-1", tot)
    assert not opslag.dpop_bewijs_eenmalig("jkt", "jti-1", tot)


def _met_anker(tmp_path: Path) -> Opslag:
    return Opslag(tmp_path / "db" / "m.db", 2, tmp_path / "anker" / "anker.log")


def test_anker_vangt_weggelaten_staart(tmp_path: Path) -> None:
    opslag = _met_anker(tmp_path)
    for i in range(4):
        opslag.audit({"beslissing": "toegestaan", "i": i})
    records = opslag.auditlog()
    anker = lees_anker(tmp_path / "anker" / "anker.log")
    assert controleer_tegen_anker(records, anker) == (True, "intact")
    assert controleer_keten(records[:-1]) == (True, None), "zonder anker valt dit niet op"
    assert controleer_tegen_anker(records[:-1], anker)[0] is False
    assert controleer_tegen_anker([], anker)[0] is False


def test_anker_vangt_herberekende_keten(tmp_path: Path) -> None:
    opslag = _met_anker(tmp_path)
    for i in range(3):
        opslag.audit({"beslissing": "toegestaan", "i": i})
    andere = _met_anker(tmp_path / "vervalst")
    for i in range(3):
        andere.audit({"beslissing": "geweigerd", "i": i})
    anker = lees_anker(tmp_path / "anker" / "anker.log")
    assert controleer_tegen_anker(andere.auditlog(), anker)[0] is False


def test_auditketen_intact_en_manipulatie_zichtbaar(opslag: Opslag, tmp_path: Path) -> None:
    for i in range(3):
        opslag.audit({"beslissing": "toegestaan", "nr_in_test": i})
    records = opslag.auditlog()
    assert controleer_keten(records) == (True, None)
    records[1]["beslissing"] = "geweigerd"
    assert controleer_keten(records) == (False, records[1]["nr"])
    weggelaten = [records[0], records[2]]
    assert controleer_keten(weggelaten)[0] is False
    assert json.dumps(records)  # records zijn serialiseerbaar voor export


def test_dpop_eenmalig_onder_gelijktijdige_lees_en_schrijfacties(opslag: Opslag) -> None:
    """Regressie: lezen zonder slot liet een hergebruikt bewijs door en gaf InterfaceError."""
    mandaat(opslag)
    tot = int(time.time()) + 60
    start = threading.Barrier(24)

    def bewijs() -> bool:
        start.wait()
        return opslag.dpop_bewijs_eenmalig("jkt", "zelfde", tot)

    def lezen() -> None:
        start.wait()
        for _ in range(50):
            opslag.gebruiker_voor_sub("sub-sanne")
            opslag.auditlog()

    with ThreadPoolExecutor(24) as pool:
        bewijzen = [pool.submit(bewijs) for _ in range(12)]
        lezers = [pool.submit(lezen) for _ in range(12)]
        uitkomsten = [f.result() for f in bewijzen]  # een fout in een draad faalt hier de test
        for f in lezers:
            f.result()
    assert sorted(uitkomsten) == [False] * 11 + [True]


def test_registratie_met_verouderde_sessiecontrole(opslag: Opslag) -> None:
    """Race: tussen sessiecontrole en registratie kwam er een mandaat bij; registratie stopt."""
    verouderd = opslag.eerdere_sessies("sub-sanne", "dossierassistent")
    a = mandaat(opslag, sid="sessie-a")
    opslag.trek_in(a["id"], "sub-sanne")
    gegevens = {
        "medewerker": "sub-sanne",
        "gebruiker": "sanne",
        "naam": "",
        "agent": "dossierassistent",
        "dossier": "2026-0143",
        "sid": "sessie-b",
    }
    with pytest.raises(HistorieGewijzigd):
        opslag.registreer_mandaat(gegevens, verouderd)


def test_ontbrekend_of_leeg_anker_is_geen_bewijs(tmp_path: Path) -> None:
    opslag = _met_anker(tmp_path)
    opslag.audit({"beslissing": "toegestaan"})
    records = opslag.auditlog()
    assert controleer_tegen_anker(records, None)[0] is False
    assert controleer_tegen_anker(records, [])[0] is False
    assert controleer_tegen_anker([], None) == (True, "intact")
    assert lees_anker(tmp_path / "bestaat-niet.log") is None
