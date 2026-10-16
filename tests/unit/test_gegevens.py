"""Rechten van de medewerker zelf: caseload en rol."""

from __future__ import annotations

import pytest

from mandaat.server.gegevens import mag_zelf


@pytest.mark.parametrize(
    ("gebruiker", "handeling", "dossier", "verwacht"),
    [
        ("sanne", "dossier:lezen", "2026-0142", None),
        ("sanne", "notitie:schrijven", "2026-0143", None),
        ("sanne", "besluit:nemen", "2026-0142", None),
        ("sanne", "dossier:lezen", "2026-0201", "dossier_niet_in_caseload_medewerker"),
        ("bram", "dossier:lezen", "2026-0142", None),
        ("bram", "notitie:schrijven", "2026-0142", "medewerker_mag_dit_zelf_niet"),
        ("bram", "dossier:lezen", "2026-0157", "dossier_niet_in_caseload_medewerker"),
        ("onbekend", "dossier:lezen", "2026-0142", "medewerker_onbekend_in_vakapplicatie"),
        ("sanne", "dossier:lezen", "9999-9999", "dossier_bestaat_niet"),
    ],
)
def test_mag_zelf(gebruiker: str, handeling: str, dossier: str, verwacht: str | None) -> None:
    assert mag_zelf(gebruiker, handeling, dossier) == verwacht
