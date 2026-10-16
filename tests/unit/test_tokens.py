"""Tokencontrole van de API, met zelf ondertekende tokens en een nagebootste introspectie."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mandaat.config import Instellingen
from mandaat.dpop import DPoPSleutel
from mandaat.server.tokens import Tokencontrole, TokenFout

INST = Instellingen(
    keycloak_url="http://keycloak:8080",
    realm="lindevoort",
    resource_url="http://dossier-mcp:8000/mcp",
    geheimen_dir=Path("/onbestaand"),
    db_pad=Path("/onbestaand/db"),
    max_skew=2,
)
SLEUTEL = rsa.generate_private_key(public_exponent=65537, key_size=2048)
DPOP = DPoPSleutel.nieuw()


class _Sleutel:
    key = SLEUTEL.public_key()


class _Antwoord:
    def __init__(self, status: int, inhoud: dict[str, Any]) -> None:
        self.status_code = status
        self._inhoud = inhoud

    def json(self) -> dict[str, Any]:
        return self._inhoud


@pytest.fixture
def controle(monkeypatch: pytest.MonkeyPatch) -> Tokencontrole:
    c = Tokencontrole(INST, "geheim")
    monkeypatch.setattr(c._jwks, "get_signing_key_from_jwt", lambda _t: _Sleutel())
    monkeypatch.setattr(httpx2, "post", lambda *a, **k: _Antwoord(200, {"active": True}))
    return c


def token(**anders: Any) -> str:
    nu = int(time.time())
    claims = {
        "iss": INST.issuer,
        "aud": INST.resource_url,
        "sub": "sub-sanne",
        "iat": nu,
        "exp": nu + 60,
        "jti": f"trrtte:{uuid.uuid4()}",
        "azp": "dossierassistent",
        "scope": "dossier:lezen",
        "act": {"sub": "sub-agent", "client_id": "dossierassistent"},
        "cnf": {"jkt": DPOP.jkt},
    }
    claims.update(anders)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, SLEUTEL, "RS256")


def agent(controle: Tokencontrole, t: str, sleutel: DPoPSleutel = DPOP) -> dict[str, Any]:
    bewijs = sleutel.bewijs("POST", INST.resource_url, access_token=t)
    return controle.agenttoken(f"DPoP {t}", bewijs, "POST").claims


def test_geldig_agenttoken(controle: Tokencontrole) -> None:
    assert agent(controle, token())["act"]["client_id"] == "dossierassistent"


@pytest.mark.parametrize(
    ("anders", "melding"),
    [
        ({"aud": "http://elders/mcp"}, "(?i)audience"),
        ({"iss": "http://nep/realms/lindevoort"}, "issuer"),
        ({"exp": int(time.time()) - 120}, "expired"),
        ({"act": None}, "act"),
        ({"act": {"client_id": "onbekende-agent"}}, "act"),
        ({"cnf": None}, "DPoP-gebonden"),
        ({"jti": None}, "jti"),
    ],
)
def test_ongeldig_agenttoken(controle: Tokencontrole, anders: dict[str, Any], melding: str) -> None:
    with pytest.raises(TokenFout, match=melding):
        agent(controle, token(**anders))


def test_bearer_schema_voor_tools_geweigerd(controle: Tokencontrole) -> None:
    with pytest.raises(TokenFout, match="DPoP"):
        controle.agenttoken(f"Bearer {token()}", None, "POST")


def test_gestolen_token_met_andere_sleutel(controle: Tokencontrole) -> None:
    with pytest.raises(TokenFout, match=r"cnf\.jkt"):
        agent(controle, token(), DPoPSleutel.nieuw())


def test_ingetrokken_token(controle: Tokencontrole, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx2, "post", lambda *a, **k: _Antwoord(200, {"active": False}))
    with pytest.raises(TokenFout, match="niet meer actief") as fout:
        agent(controle, token())
    assert fout.value.claims["sub"] == "sub-sanne"


def test_introspectie_onbereikbaar_faalt_dicht(
    controle: Tokencontrole, monkeypatch: pytest.MonkeyPatch
) -> None:
    def kapot(*_a: Any, **_k: Any) -> None:
        raise httpx2.ConnectError("Keycloak onbereikbaar")

    monkeypatch.setattr(httpx2, "post", kapot)
    with pytest.raises(TokenFout, match="introspectie niet mogelijk"):
        agent(controle, token())


def _medewerker(**anders: Any) -> str:
    standaard = {"act": None, "cnf": None, "azp": "werkplek", "scope": "mandaat:beheren"}
    return token(**{**standaard, **anders})


def test_medewerkertoken(controle: Tokencontrole) -> None:
    claims = controle.medewerkertoken(f"Bearer {_medewerker()}", "mandaat:beheren").claims
    assert claims["azp"] == "werkplek"


@pytest.mark.parametrize(
    ("anders", "melding", "status"),
    [
        ({"act": {"client_id": "dossierassistent"}}, "niet voor een agent", 401),
        ({"may_act": {"client_id": "dossierassistent"}}, "niet voor een agent", 401),
        ({"azp": "dossierassistent"}, "werkplek", 401),
        ({"scope": "dossier:lezen"}, "mandaat:beheren", 403),
    ],
)
def test_ongeldig_medewerkertoken(
    controle: Tokencontrole, anders: dict[str, Any], melding: str, status: int
) -> None:
    with pytest.raises(TokenFout, match=melding) as fout:
        controle.medewerkertoken(f"Bearer {_medewerker(**anders)}", "mandaat:beheren")
    assert fout.value.status == status
