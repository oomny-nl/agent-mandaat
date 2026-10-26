"""Het meetharnas tegen de echte keten, met de deterministische planners (zonder model)."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mandaat.agent.makelaar import Makelaar
from mandaat.config import Instellingen
from mandaat.meting import Budget, Meting, analyse, lees, meet
from mandaat.mulder import VERGISSING

pytestmark = pytest.mark.integratie


def _notities(inst: Instellingen) -> int:
    db = sqlite3.connect(f"file:{inst.db_pad}?mode=ro", uri=True)
    try:
        return db.execute(
            "SELECT COUNT(*) FROM notities WHERE dossier = ?", (VERGISSING,)
        ).fetchone()[0]
    finally:
        db.close()


def test_meetharnas_doorzetter_en_vrager(
    verleen: Any, makelaar: Makelaar, inst: Instellingen, tmp_path: Path
) -> None:
    sessie = verleen(taak=VERGISSING)
    voor = _notities(inst)
    pad = tmp_path / "runs.jsonl"
    meting = Meting(makelaar, sessie.werkplek, Budget("deterministisch"), None)
    uitkomst = asyncio.run(meet(meting, pad, 6))
    assert uitkomst.startswith("klaar: 6 runs")
    records = {r["sleutel"]: r for r in lees(pad)}
    for parafrase in ("00", "01"):
        assert records[f"A-{parafrase}-1"]["eerste_actie"] == "schrijft_0162"
        assert records[f"B-{parafrase}-1"]["eerste_actie"] == "vraagt"
        assert not records[f"C-{parafrase}-1"]["schrijft_0162"]
    assert all(r["modelaanroepen"] >= 2 for r in records.values())  # lezen, dan handelen
    assert "| A | 2 | 0 | 2/2" in analyse(list(records.values()))
    # De onderschepte notities hebben het dossier niet bereikt: elke run leest hetzelfde dossier.
    assert _notities(inst) == voor
