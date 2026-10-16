"""De deterministische planner volgt ook ingevoegde instructies (gesimuleerde kaping)."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from mandaat.agent.planner import deterministische_planner
from mandaat.server.gegevens import DOSSIERS


def _antwoord(oproep: Any, inhoud: str, status: str = "success") -> ToolMessage:
    return ToolMessage(
        inhoud,
        tool_call_id=oproep["id"],
        name=oproep["name"],
        status=status,
        additional_kwargs={"dossier": oproep["args"].get("dossier", "")},
    )


def _speel(dossier: str, weigeren: set[str]) -> list[tuple[str, str]]:
    berichten: list[AnyMessage] = [HumanMessage(f"Vat dossier {dossier} samen.")]
    stappen = []
    for _ in range(10):
        bericht = deterministische_planner(berichten)
        berichten.append(bericht)
        if not bericht.tool_calls:
            break
        for oproep in bericht.tool_calls:
            stappen.append((oproep["name"], oproep["args"]["dossier"]))
            if oproep["name"] in weigeren or oproep["args"]["dossier"] != dossier:
                berichten.append(_antwoord(oproep, "GEWEIGERD", "error"))
            else:
                berichten.append(_antwoord(oproep, json.dumps(DOSSIERS[dossier])))
    assert isinstance(berichten[-1], AIMessage)
    return stappen


def test_gewone_taak() -> None:
    assert _speel("2026-0142", set()) == [
        ("dossier_lezen", "2026-0142"),
        ("notitie_toevoegen", "2026-0142"),
    ]


def test_gekaapte_taak_probeert_besluit_en_ander_dossier() -> None:
    stappen = _speel("2026-0143", {"besluit_vastleggen"})
    assert ("besluit_vastleggen", "2026-0143") in stappen
    assert ("dossier_lezen", "2026-0157") in stappen
    assert stappen[-1] == ("notitie_toevoegen", "2026-0143")


def test_zonder_dossiernummer() -> None:
    antwoord = deterministische_planner([HumanMessage("Doe iets.")])
    assert not antwoord.tool_calls
