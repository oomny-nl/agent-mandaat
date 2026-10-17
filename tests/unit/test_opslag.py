"""Mandaatregister, generatiebinding, eenmalig gebruik en auditketen."""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from mandaat.server.opslag import Opslag, controleer_keten, controleer_tegen_anker, lees_anker

ISS = "http://keycloak:8080/realms/lindevoort"


@pytest.fixture
def opslag(tmp_path: Path) -> Opslag:
    return Opslag(tmp_path / "mandaat.db", max_skew=2)


def mandaat(opslag: Opslag, dossier: str = "2026-0142", sid: str | None = None) -> dict[str, Any]:
    return opslag.registreer_mandaat(
        {
            "medewerker": "sub-sanne",
            "gebruiker": "sanne",
            "naam": "Sanne de Vries",
            "agent": "dossierassistent",
            "dossier": dossier,
            "sid": sid or str(uuid.uuid4()),
        }
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
    uitkomsten: list[bool] = []
    start = threading.Barrier(16)

    def poging() -> None:
        start.wait()
        uitkomsten.append(opslag.laat_toe(c, "2026-0142").toegestaan)

    draden = [threading.Thread(target=poging) for _ in range(16)]
    for d in draden:
        d.start()
    for d in draden:
        d.join()
    assert uitkomsten.count(True) == 1


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
    uitkomsten: list[bool] = []
    fouten: list[BaseException] = []
    start = threading.Barrier(24)

    def bewijs() -> None:
        start.wait()
        uitkomsten.append(opslag.dpop_bewijs_eenmalig("jkt", "zelfde", tot))

    def lezen() -> None:
        start.wait()
        try:
            for _ in range(50):
                opslag.gebruiker_voor_sub("sub-sanne")
                opslag.auditlog()
        except BaseException as fout:
            fouten.append(fout)

    draden = [threading.Thread(target=bewijs) for _ in range(12)]
    draden += [threading.Thread(target=lezen) for _ in range(12)]
    for d in draden:
        d.start()
    for d in draden:
        d.join()
    assert fouten == []
    assert uitkomsten.count(True) == 1
