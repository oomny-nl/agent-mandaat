"""Afdwinging in de dossier-API: mens, agent, taak, scope, DPoP en de bypassgevallen."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx2
import pytest

from mandaat.agent.makelaar import Makelaar
from mandaat.config import Instellingen
from mandaat.dpop import DPoPSleutel
from mandaat.werkplek.sessie import Werkplek, WerkplekFout
from tests.integratie.conftest import MCP_KOPPEN, roep, toolfout

pytestmark = pytest.mark.integratie


def test_protected_resource_metadata(inst: Instellingen) -> None:
    basis, pad = inst.resource_url.rsplit("/", 1)
    meta = httpx2.get(f"{basis}/.well-known/oauth-protected-resource/{pad}", timeout=10).json()
    assert meta["resource"] == inst.resource_url
    assert meta["authorization_servers"] == [inst.issuer]
    assert meta["dpop_bound_access_tokens_required"] is True


def test_zonder_token_dpop_challenge(inst: Instellingen) -> None:
    antwoord = httpx2.post(inst.resource_url, json={}, headers=MCP_KOPPEN, timeout=10)
    assert antwoord.status_code == 401
    kop = antwoord.headers["www-authenticate"]
    assert kop.startswith("DPoP ")
    assert "resource_metadata=" in kop


def test_toegestaan_binnen_mandaat(verleen: Any, makelaar: Makelaar, inst: Instellingen) -> None:
    sessie = verleen()
    token = makelaar.gedelegeerd(sessie.subject_token, "dossier:lezen")
    antwoord = roep(inst, token, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop)
    assert toolfout(antwoord) is None


def test_dossier_buiten_mandaat(verleen: Any, makelaar: Makelaar, inst: Instellingen) -> None:
    token = makelaar.gedelegeerd(verleen().subject_token, "dossier:lezen")
    antwoord = roep(inst, token, "dossier_lezen", {"dossier": "2026-0157"}, makelaar.dpop)
    assert toolfout(antwoord) == "dossier_buiten_mandaat"


def test_agent_krijgt_nooit_meer_rechten_dan_de_mens(
    verleen: Any, makelaar: Makelaar, inst: Instellingen
) -> None:
    """Bram (intake) mag zelf geen notities schrijven; zijn agent dus ook niet."""
    sessie = verleen("bram", "2026-0142")
    lezen = makelaar.gedelegeerd(sessie.subject_token, "dossier:lezen")
    assert (
        toolfout(roep(inst, lezen, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop))
        is None
    )
    schrijven = makelaar.gedelegeerd(sessie.subject_token, "notitie:schrijven")
    antwoord = roep(
        inst, schrijven, "notitie_toevoegen", {"dossier": "2026-0142", "tekst": "x"}, makelaar.dpop
    )
    assert toolfout(antwoord) == "medewerker_mag_dit_zelf_niet"


def test_mandaat_alleen_voor_eigen_caseload(inst: Instellingen, geheimen: Any) -> None:
    werkplek = Werkplek(inst, "bram", geheimen.wachtwoord("bram"))
    werkplek.login("2026-0157")
    try:
        with pytest.raises(WerkplekFout, match="caseload"):
            werkplek.registreer_mandaat()
    finally:
        werkplek.log_uit()


def test_scope_per_tool(verleen: Any, makelaar: Makelaar, inst: Instellingen) -> None:
    token = makelaar.gedelegeerd(verleen().subject_token, "dossier:lezen")
    antwoord = roep(
        inst, token, "notitie_toevoegen", {"dossier": "2026-0142", "tekst": "x"}, makelaar.dpop
    )
    assert antwoord.status_code == 403
    assert 'scope="notitie:schrijven"' in antwoord.headers["www-authenticate"]


def test_tweede_laag_agent_legt_nooit_besluit_vast(
    verleen: Any, makelaar: Makelaar, inst: Instellingen, admin: httpx2.Client
) -> None:
    """Zelfs met een geldig ondertekend token mét besluit:nemen weigert de API de agent."""
    pad = f"/{inst.realm}/clients"
    agent = admin.get(pad, params={"clientId": inst.agent_id}).json()[0]
    scopes = {s["name"]: s["id"] for s in admin.get(f"/{inst.realm}/client-scopes").json()}
    koppel = f"{pad}/{agent['id']}/optional-client-scopes/{scopes['besluit:nemen']}"
    assert admin.put(koppel).status_code == 204
    try:
        token = makelaar.gedelegeerd(verleen().subject_token, "besluit:nemen")
        antwoord = roep(
            inst,
            token,
            "besluit_vastleggen",
            {"dossier": "2026-0142", "uitkomst": "toekennen"},
            makelaar.dpop,
        )
        assert toolfout(antwoord) == "gedelegeerde_partij_legt_geen_besluit_vast"
    finally:
        admin.delete(koppel)


def test_token_eenmalig(verleen: Any, makelaar: Makelaar, inst: Instellingen) -> None:
    token = makelaar.gedelegeerd(verleen().subject_token, "dossier:lezen")
    assert (
        toolfout(roep(inst, token, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop))
        is None
    )
    tweede = roep(inst, token, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop)
    assert toolfout(tweede) == "token_al_gebruikt"


def test_gelijktijdig_hetzelfde_token(verleen: Any, makelaar: Makelaar, inst: Instellingen) -> None:
    token = makelaar.gedelegeerd(verleen().subject_token, "dossier:lezen")

    async def poging() -> str | None:
        return await asyncio.to_thread(
            lambda: toolfout(
                roep(inst, token, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop)
            )
        )

    async def alle() -> list[str | None]:
        return list(await asyncio.gather(*(poging() for _ in range(6))))

    uitkomsten = asyncio.run(alle())
    assert uitkomsten.count(None) == 1
    assert uitkomsten.count("token_al_gebruikt") == 5


def test_gestolen_token_zonder_dpop_sleutel(
    verleen: Any, makelaar: Makelaar, inst: Instellingen
) -> None:
    token = makelaar.gedelegeerd(verleen().subject_token, "dossier:lezen")
    dief = DPoPSleutel.nieuw()
    antwoord = roep(inst, token, "dossier_lezen", {"dossier": "2026-0142"}, dief)
    assert antwoord.status_code == 401
    assert 'error="invalid_token"' in antwoord.headers["www-authenticate"]


def test_gestolen_token_als_bearer(verleen: Any, makelaar: Makelaar, inst: Instellingen) -> None:
    token = makelaar.gedelegeerd(verleen().subject_token, "dossier:lezen")
    antwoord = roep(
        inst, token, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop, schema="Bearer"
    )
    assert antwoord.status_code == 401


@pytest.mark.parametrize(
    "opties",
    [{"htu": "http://elders:8000/mcp"}, {"htm": "GET"}],
)
def test_dpop_bewijs_voor_ander_verzoek(
    verleen: Any, makelaar: Makelaar, inst: Instellingen, opties: dict[str, str]
) -> None:
    token = makelaar.gedelegeerd(verleen().subject_token, "dossier:lezen")
    antwoord = roep(inst, token, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop, **opties)
    assert antwoord.status_code == 401
    assert 'error="invalid_dpop_proof"' in antwoord.headers["www-authenticate"]


def test_hergebruikt_dpop_bewijs(verleen: Any, makelaar: Makelaar, inst: Instellingen) -> None:
    sessie = verleen()
    eerste = makelaar.gedelegeerd(sessie.subject_token, "dossier:lezen")
    bewijs = makelaar.dpop.bewijs("POST", inst.resource_url, access_token=eerste.waarde)
    assert (
        roep(
            inst, eerste, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop, bewijs=bewijs
        ).status_code
        == 200
    )
    tweede = roep(
        inst, eerste, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop, bewijs=bewijs
    )
    assert tweede.status_code == 401


def test_subject_token_direct_op_tools(
    verleen: Any, makelaar: Makelaar, inst: Instellingen
) -> None:
    sessie = verleen()
    antwoord = roep(
        inst, sessie.subject_token, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop
    )
    assert antwoord.status_code == 401


def test_actortoken_direct_op_tools(makelaar: Makelaar, inst: Instellingen) -> None:
    antwoord = roep(
        inst, makelaar.actortoken(), "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop
    )
    assert antwoord.status_code == 401


def test_agent_kan_geen_mandaat_beheren(
    verleen: Any, makelaar: Makelaar, inst: Instellingen
) -> None:
    """Met geen enkel token dat de agent heeft, kan hij een mandaat aanmaken of intrekken."""
    sessie = verleen()
    api = inst.resource_url.rsplit("/", 1)[0]
    gedelegeerd = makelaar.gedelegeerd(sessie.subject_token, "dossier:lezen").waarde
    for token in (sessie.subject_token, makelaar.actortoken(), gedelegeerd):
        aanmaken = httpx2.post(
            f"{api}/mandaten", headers={"Authorization": f"Bearer {token}"}, timeout=10
        )
        intrekken = httpx2.delete(
            f"{api}/mandaten/{sessie.mandaat['id']}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        assert aanmaken.status_code == 401, aanmaken.text
        assert intrekken.status_code == 401, intrekken.text
        assert aanmaken.headers["www-authenticate"].startswith("Bearer ")
