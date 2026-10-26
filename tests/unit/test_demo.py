"""De demo faalt als een scenario een andere uitkomst geeft dan het ontwerp."""

from __future__ import annotations

import pytest

from mandaat import demo


def test_verwachte_volgorde_slaagt() -> None:
    demo.GEBEURTENISSEN[:] = [
        {"soort": "keycloak_weigert", "scope": "besluit:nemen"},
        {"soort": "api_weigert", "tekst": "GEWEIGERD: dossier_buiten_mandaat"},
        {"soort": "resultaat", "tool": "notitie_toevoegen"},
    ]
    demo.verwacht(
        "scenario 2",
        [
            ("keycloak_weigert", "besluit:nemen"),
            ("api_weigert", "dossier_buiten_mandaat"),
            ("resultaat", "notitie_toevoegen"),
        ],
    )
    assert demo.GEBEURTENISSEN == []


def test_afwijking_laat_demo_falen() -> None:
    """Een toegestane actie waar een weigering hoorde, stopt de demo."""
    demo.GEBEURTENISSEN[:] = [{"soort": "resultaat", "tool": "dossier_lezen", "tekst": "2026-0157"}]
    with pytest.raises(demo.DemoAfwijking, match="dossier_buiten_mandaat"):
        demo.verwacht("scenario 2", [("api_weigert", "dossier_buiten_mandaat")])


def test_verkeerde_volgorde_laat_demo_falen() -> None:
    demo.GEBEURTENISSEN[:] = [
        {"soort": "resultaat", "tool": "notitie_toevoegen"},
        {"soort": "keycloak_weigert", "scope": "besluit:nemen"},
    ]
    with pytest.raises(demo.DemoAfwijking):
        demo.verwacht(
            "scenario 2",
            [("keycloak_weigert", "besluit:nemen"), ("resultaat", "notitie_toevoegen")],
        )


def test_verboden_gebeurtenis_laat_demo_falen() -> None:
    """De vrager in scenario 4b mag niets schrijven, ook niet naast de verwachte vraag."""
    demo.GEBEURTENISSEN[:] = [
        {"soort": "resultaat", "tool": "dossier_lezen"},
        {"soort": "aanroep", "tool": "notitie_toevoegen", "argumenten": {"dossier": "2026-0162"}},
        {"soort": "antwoord", "tekst": "2026-0161"},
    ]
    with pytest.raises(demo.DemoAfwijking, match="hoort hier niet"):
        demo.verwacht(
            "scenario 4b",
            [("resultaat", "dossier_lezen"), ("antwoord", "2026-0161")],
            verboden=(("aanroep", "notitie_toevoegen"),),
        )
