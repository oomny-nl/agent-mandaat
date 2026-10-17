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
