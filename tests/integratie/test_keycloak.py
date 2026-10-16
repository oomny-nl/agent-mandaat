"""Gedrag van Keycloak 26.8.1 waarop het ontwerp leunt (ONTWERP.md sectie 3, proeven 1 tot 21)."""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from mandaat.agent.makelaar import ASSERTION_TYPE, Makelaar, MandaatGeweigerd
from mandaat.config import Instellingen
from mandaat.geheimen import AGENT_KID
from mandaat.werkplek.sessie import WerkplekFout

pytestmark = pytest.mark.integratie


def lees(token: str) -> dict[str, Any]:
    """Claims lezen voor inspectie; de handtekening controleert de API, niet deze test."""
    return jwt.decode(token, options={"verify_signature": False})


def test_subject_token_heeft_may_act_en_alleen_de_agent_als_audience(verleen: Any) -> None:
    sessie = verleen()
    claims = lees(sessie.subject_token)
    assert claims["may_act"]["client_id"] == "dossierassistent"
    assert claims["aud"] == "dossierassistent"
    assert "mandaat:beheren" not in claims["scope"]
    assert claims["taak"] == "2026-0142"


def test_beheertoken_zonder_delegatie(verleen: Any, inst: Instellingen) -> None:
    claims = lees(verleen().werkplek.beheertoken())
    assert "may_act" not in claims
    assert "act" not in claims
    assert "mandaat:beheren" in claims["scope"].split()
    assert inst.resource_url in (
        [claims["aud"]] if isinstance(claims["aud"], str) else claims["aud"]
    )


def test_gedelegeerd_token_is_smal_kort_en_gebonden(
    verleen: Any, makelaar: Makelaar, inst: Instellingen
) -> None:
    token = makelaar.gedelegeerd(verleen().subject_token, "dossier:lezen")
    claims = lees(token.waarde)
    assert claims["act"]["client_id"] == "dossierassistent"
    assert claims["aud"] == inst.resource_url
    assert set(claims["scope"].split()) == {"dossier:lezen"}
    assert claims["exp"] - claims["iat"] == 60
    assert claims["cnf"]["jkt"] == makelaar.dpop.jkt
    assert "taak" not in claims
    assert "sid" not in claims


def test_exchange_geeft_geen_refresh_token(verleen: Any, makelaar: Makelaar) -> None:
    antwoord = makelaar._post(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": verleen().subject_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "actor_token": makelaar.actortoken(),
            "actor_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "scope": "dossier:lezen",
        }
    )
    assert "refresh_token" not in antwoord


def test_besluitscope_bestaat_niet_voor_de_agent(verleen: Any, makelaar: Makelaar) -> None:
    with pytest.raises(MandaatGeweigerd) as fout:
        makelaar.gedelegeerd(verleen().subject_token, "besluit:nemen")
    assert fout.value.fout == "invalid_scope"


def test_agentketen_geweigerd(verleen: Any, makelaar: Makelaar) -> None:
    gedelegeerd = makelaar.gedelegeerd(verleen().subject_token, "dossier:lezen")
    with pytest.raises(MandaatGeweigerd, match="may_act"):
        makelaar.gedelegeerd(gedelegeerd.waarde, "dossier:lezen")


def test_exchange_zonder_dpop_bewijs_geweigerd(
    verleen: Any, makelaar: Makelaar, inst: Instellingen
) -> None:
    sessie = verleen()
    antwoord = httpx2.post(
        makelaar.meta.token_endpoint,
        data={
            "client_id": inst.agent_id,
            "client_assertion_type": ASSERTION_TYPE,
            "client_assertion": makelaar._assertion(),
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": sessie.subject_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "actor_token": makelaar.actortoken(),
            "actor_token_type": "urn:ietf:params:oauth:token-type:access_token",
        },
        timeout=30,
    )
    assert antwoord.status_code == 400
    assert "DPoP" in antwoord.json()["error_description"]


def test_agent_zonder_sleutel_komt_er_niet_in(makelaar: Makelaar, inst: Instellingen) -> None:
    vreemde = ec.generate_private_key(ec.SECP256R1())
    nu = int(time.time())
    assertion = jwt.encode(
        {
            "iss": inst.agent_id,
            "sub": inst.agent_id,
            "aud": makelaar.meta.issuer,
            "jti": str(uuid.uuid4()),
            "iat": nu,
            "exp": nu + 60,
        },
        vreemde,
        algorithm="ES256",
        headers={"kid": AGENT_KID},
    )
    antwoord = httpx2.post(
        makelaar.meta.token_endpoint,
        data={
            "grant_type": "client_credentials",
            "client_id": inst.agent_id,
            "client_assertion_type": ASSERTION_TYPE,
            "client_assertion": assertion,
        },
        headers={"DPoP": makelaar.dpop.bewijs("POST", makelaar.meta.token_endpoint)},
        timeout=30,
    )
    assert antwoord.status_code in (400, 401)
    assert antwoord.json()["error"] == "invalid_client"


def test_may_act_executor_weigert_ongeconsenteerde_may_act(
    admin: httpx2.Client, inst: Instellingen, verleen: Any
) -> None:
    """Een hardcoded `may_act`-mapper omzeilt consent en FGAP; de client policy vangt dat af."""
    pad = f"/{inst.realm}/clients"
    werkplek = admin.get(pad, params={"clientId": inst.werkplek_id}).json()[0]
    mapper = {
        "name": "vals-may-act",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-hardcoded-claim-mapper",
        "config": {
            "claim.name": "may_act.client_id",
            "claim.value": "dossierassistent",
            "jsonType.label": "String",
            "access.token.claim": "true",
        },
    }
    antwoord = admin.post(f"{pad}/{werkplek['id']}/protocol-mappers/models", json=mapper)
    assert antwoord.status_code == 201, antwoord.text
    mapper_id = antwoord.headers["location"].rsplit("/", 1)[-1]
    try:
        with pytest.raises(WerkplekFout, match=r"tokenverzoek geweigerd"):
            verleen()
    finally:
        admin.delete(f"{pad}/{werkplek['id']}/protocol-mappers/models/{mapper_id}")
