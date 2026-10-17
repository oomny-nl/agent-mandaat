"""Aanvullende proeven: vervanging, RFC 7009, FGAP, introspectie-audience, DPoP-negatieven."""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx2
import jwt
import pytest

from mandaat.agent.makelaar import (
    ACCESS_TOKEN_TYPE,
    ASSERTION_TYPE,
    TOKEN_EXCHANGE,
    Makelaar,
    MandaatGeweigerd,
)
from mandaat.config import Instellingen
from mandaat.dpop import token_hash
from mandaat.geheimen import Geheimen
from mandaat.werkplek.sessie import Werkplek
from tests.integratie.conftest import MCP_KOPPEN, roep, toolfout

pytestmark = pytest.mark.integratie


def lees(token: str) -> dict[str, Any]:
    """Claims lezen voor inspectie; de API controleert de handtekening."""
    return jwt.decode(token, options={"verify_signature": False})


def test_vervanging_vereist_beeindigde_oude_sessie(
    inst: Instellingen, geheimen: Geheimen, makelaar: Makelaar
) -> None:
    """A ingetrokken, sessie A leeft nog: mandaat B wordt geweigerd tot A is uitgelogd."""
    api = inst.resource_url.rsplit("/", 1)[0]
    a = Werkplek(inst, "sanne", geheimen.wachtwoord("sanne"))
    a.login("2026-0142")
    mandaat_a = a.registreer_mandaat()
    subject_a = a.subject_token()
    ingetrokken = httpx2.delete(
        f"{api}/mandaten/{mandaat_a['id']}",
        headers={"Authorization": f"Bearer {a.beheertoken()}"},
        timeout=10,
    )
    assert ingetrokken.status_code == 200  # intrekken, maar (nog) niet uitloggen
    b = Werkplek(inst, "sanne", geheimen.wachtwoord("sanne"))
    b.login("2026-0143")
    mandaat_b: dict[str, Any] | None = None
    try:
        poging = httpx2.post(
            f"{api}/mandaten", headers={"Authorization": f"Bearer {b.beheertoken()}"}, timeout=10
        )
        assert poging.status_code == 409, poging.text
        a.log_uit()
        mandaat_b = b.registreer_mandaat()
        assert mandaat_b["dossier"] == "2026-0143"
        with pytest.raises(MandaatGeweigerd):
            makelaar.gedelegeerd(subject_a, "dossier:lezen")
    finally:
        a.log_uit()
        if mandaat_b is not None:
            b.trek_in(mandaat_b["id"])
        b.log_uit()


def test_rfc7009_intrekking_van_een_token(
    verleen: Any, makelaar: Makelaar, inst: Instellingen
) -> None:
    sessie = verleen()
    token = makelaar.gedelegeerd(sessie.subject_token, "dossier:lezen")
    antwoord = httpx2.post(
        f"{inst.oidc}/revoke",
        data={
            "token": token.waarde,
            "token_type_hint": "access_token",
            "client_id": inst.agent_id,
            "client_assertion_type": ASSERTION_TYPE,
            "client_assertion": makelaar._assertion(),
        },
        headers={"DPoP": makelaar.dpop.bewijs("POST", f"{inst.oidc}/revoke")},
        timeout=10,
    )
    assert antwoord.status_code == 200, antwoord.text
    geweigerd = roep(inst, token, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop)
    assert geweigerd.status_code == 401
    nieuw = makelaar.gedelegeerd(sessie.subject_token, "dossier:lezen")
    assert (
        toolfout(roep(inst, nieuw, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop))
        is None
    )


@pytest.fixture
def zonder_delegatiepermissie(admin: httpx2.Client, inst: Instellingen) -> Iterator[None]:
    r = inst.realm
    ap = admin.get(f"/{r}/clients", params={"clientId": "admin-permissions"}).json()[0]["id"]
    basis = f"/{r}/clients/{ap}/authz/resource-server"
    permissie = next(p for p in admin.get(f"{basis}/permission").json() if "namens" in p["name"])
    volledig = admin.get(f"{basis}/permission/scope/{permissie['id']}").json()
    beleid = [
        p["id"]
        for p in admin.get(f"{basis}/permission/{permissie['id']}/associatedPolicies").json()
    ]
    assert admin.delete(f"{basis}/permission/{permissie['id']}").status_code == 204
    yield
    herstel = {
        "name": volledig["name"],
        "resourceType": "Users",
        "scopes": ["delegate"],
        "policies": beleid,
        "decisionStrategy": "UNANIMOUS",
    }
    assert admin.post(f"{basis}/permission/scope", json=herstel).status_code == 201


def test_fgap_wijziging_geldt_pas_na_nieuwe_login(
    verleen: Any,
    makelaar: Makelaar,
    inst: Instellingen,
    geheimen: Geheimen,
    zonder_delegatiepermissie: None,
) -> None:
    """Gemeten beperking (ONTWERP proef 12): een bestaand subject token kan nog inwisselen."""
    del zonder_delegatiepermissie
    nieuw = Werkplek(inst, "bram", geheimen.wachtwoord("bram"))
    nieuw.login("2026-0142")
    try:
        assert "may_act" not in lees(nieuw.subject_token())
    finally:
        nieuw.log_uit()


def test_bestaand_subject_token_na_fgap_wijziging(
    verleen: Any, makelaar: Makelaar, admin: httpx2.Client, inst: Instellingen
) -> None:
    sessie = verleen()
    r = inst.realm
    ap = admin.get(f"/{r}/clients", params={"clientId": "admin-permissions"}).json()[0]["id"]
    basis = f"/{r}/clients/{ap}/authz/resource-server"
    permissie = next(p for p in admin.get(f"{basis}/permission").json() if "namens" in p["name"])
    beleid = [
        p["id"]
        for p in admin.get(f"{basis}/permission/{permissie['id']}/associatedPolicies").json()
    ]
    admin.delete(f"{basis}/permission/{permissie['id']}")
    try:
        token = makelaar.gedelegeerd(sessie.subject_token, "dossier:lezen")
        assert lees(token.waarde)["act"]["client_id"] == inst.agent_id
    finally:
        admin.post(
            f"{basis}/permission/scope",
            json={
                "name": permissie["name"],
                "resourceType": "Users",
                "scopes": ["delegate"],
                "policies": beleid,
                "decisionStrategy": "UNANIMOUS",
            },
        )


def test_exchange_zonder_actortoken(verleen: Any, makelaar: Makelaar) -> None:
    with pytest.raises(MandaatGeweigerd, match="delegation claim"):
        makelaar._post(
            {
                "grant_type": TOKEN_EXCHANGE,
                "subject_token": verleen().subject_token,
                "subject_token_type": ACCESS_TOKEN_TYPE,
                "scope": "dossier:lezen",
            }
        )


def test_introspectie_door_client_buiten_audience(
    verleen: Any, makelaar: Makelaar, admin: httpx2.Client, inst: Instellingen
) -> None:
    token = makelaar.gedelegeerd(verleen().subject_token, "dossier:lezen")
    geheim = str(uuid.uuid4())
    naam = f"buitenstaander-{uuid.uuid4().hex[:6]}"
    admin.post(
        f"/{inst.realm}/clients",
        json={
            "clientId": naam,
            "secret": geheim,
            "publicClient": False,
            "standardFlowEnabled": False,
        },
    )
    try:
        antwoord = httpx2.post(
            f"{inst.oidc}/token/introspect",
            data={"token": token.waarde, "client_id": naam, "client_secret": geheim},
            timeout=10,
        ).json()
        assert antwoord["active"] is False
    finally:
        intern = admin.get(f"/{inst.realm}/clients", params={"clientId": naam}).json()[0]["id"]
        admin.delete(f"/{inst.realm}/clients/{intern}")


def _bewijs(makelaar: Makelaar, inst: Instellingen, waarde: str, **anders: Any) -> str:
    claims = {
        "jti": str(uuid.uuid4()),
        "htm": "POST",
        "htu": inst.resource_url,
        "iat": int(time.time()),
        "ath": token_hash(waarde),
    }
    kop = {"typ": "dpop+jwt", "jwk": makelaar.dpop.publieke_jwk, **anders.pop("kop", {})}
    claims.update(anders)
    return jwt.encode(claims, makelaar.dpop.sleutel_priv, algorithm="ES256", headers=kop)


@pytest.mark.parametrize(
    "anders",
    [{"iat": int(time.time()) - 600}, {"ath": token_hash("ander-token")}, {"kop": {"typ": "jwt"}}],
    ids=["verlopen", "ander-token", "verkeerd-typ"],
)
def test_dpop_negatieven_in_de_keten(
    verleen: Any, makelaar: Makelaar, inst: Instellingen, anders: dict[str, Any]
) -> None:
    token = makelaar.gedelegeerd(verleen().subject_token, "dossier:lezen")
    bewijs = _bewijs(makelaar, inst, token.waarde, **anders)
    antwoord = roep(
        inst, token, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop, bewijs=bewijs
    )
    assert antwoord.status_code == 401
    assert 'error="invalid_dpop_proof"' in antwoord.headers["www-authenticate"]


def test_symmetrisch_dpop_bewijs(verleen: Any, makelaar: Makelaar, inst: Instellingen) -> None:
    token = makelaar.gedelegeerd(verleen().subject_token, "dossier:lezen")
    claims = {
        "jti": "x",
        "htm": "POST",
        "htu": inst.resource_url,
        "iat": int(time.time()),
        "ath": token_hash(token.waarde),
    }
    bewijs = jwt.encode(
        claims, "x" * 40, "HS256", headers={"typ": "dpop+jwt", "jwk": makelaar.dpop.publieke_jwk}
    )
    antwoord = roep(
        inst, token, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop, bewijs=bewijs
    )
    assert antwoord.status_code == 401


def test_gelijktijdig_hetzelfde_bewijs(
    verleen: Any, makelaar: Makelaar, inst: Instellingen
) -> None:
    token = makelaar.gedelegeerd(verleen().subject_token, "dossier:lezen")
    bewijs = makelaar.dpop.bewijs("POST", inst.resource_url, access_token=token.waarde)

    async def alle() -> list[int]:
        pogingen = [
            asyncio.to_thread(
                lambda: (
                    roep(
                        inst,
                        token,
                        "dossier_lezen",
                        {"dossier": "2026-0142"},
                        makelaar.dpop,
                        bewijs=bewijs,
                    ).status_code
                )
            )
            for _ in range(6)
        ]
        return list(await asyncio.gather(*pogingen))

    statussen = asyncio.run(alle())
    assert statussen.count(200) == 1
    assert statussen.count(401) == 5


def test_vervalste_tokenhandtekening(verleen: Any, makelaar: Makelaar, inst: Instellingen) -> None:
    token = makelaar.gedelegeerd(verleen().subject_token, "dossier:lezen")
    kop, inhoud, handtekening = token.waarde.split(".")
    vervalst = f"{kop}.{inhoud}.{handtekening[:-6]}AAAAAA"
    antwoord = roep(inst, vervalst, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop)
    assert antwoord.status_code == 401
    assert "token ongeldig" in antwoord.headers["www-authenticate"]


def test_echte_dezelfde_seconde(verleen: Any, makelaar: Makelaar, inst: Instellingen) -> None:
    """Registratie en uitgifte vallen aantoonbaar in dezelfde seconde; het token wordt geweigerd."""
    for _ in range(6):
        sessie = verleen(wachten=False)
        token = makelaar.gedelegeerd(sessie.subject_token, "dossier:lezen")
        if lees(token.waarde)["iat"] == math.floor(sessie.mandaat["verleend_om"]):
            antwoord = roep(inst, token, "dossier_lezen", {"dossier": "2026-0142"}, makelaar.dpop)
            assert toolfout(antwoord) == "mandaat_nog_niet_geldig"
            return
        sessie.werkplek.trek_in(sessie.mandaat["id"])
    pytest.fail("in zes pogingen geen uitgifte in dezelfde seconde als de registratie")


def test_misvormd_verzoek_zonder_token(inst: Instellingen) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["x"]}
    antwoord = httpx2.post(inst.resource_url, json=body, headers=MCP_KOPPEN, timeout=10)
    assert antwoord.status_code == 401
    groot = httpx2.post(inst.resource_url, content=b"x" * 70000, headers=MCP_KOPPEN, timeout=10)
    assert groot.status_code == 413


def _audit(inst: Instellingen) -> list[dict[str, Any]]:
    db = sqlite3.connect(f"file:{inst.db_pad}?mode=ro", uri=True)
    return [json.loads(r[0]) for r in db.execute("SELECT record FROM audit ORDER BY nr")]


def test_beheerweigeringen_staan_in_de_audit(
    verleen: Any, inst: Instellingen, geheimen: Geheimen
) -> None:
    """Bram trekt het mandaat van Sanne in: 404, en de weigering staat met zijn naam in de audit."""
    sanne = verleen()
    bram = Werkplek(inst, "bram", geheimen.wachtwoord("bram"))
    bram.login("2026-0142")
    try:
        api = inst.resource_url.rsplit("/", 1)[0]
        antwoord = httpx2.delete(
            f"{api}/mandaten/{sanne.mandaat['id']}",
            headers={"Authorization": f"Bearer {bram.beheertoken()}"},
            timeout=10,
        )
        assert antwoord.status_code == 404
    finally:
        bram.log_uit()
    records = _audit(inst)
    assert any(
        r.get("laag") == "api-beheer"
        and r.get("gebruiker") == "bram"
        and r["beslissing"] == "geweigerd"
        and sanne.mandaat["id"] in str(r.get("tool"))
        for r in records
    )
    assert any("sessie van een eerder mandaat" in str(r.get("reden")) for r in records)
