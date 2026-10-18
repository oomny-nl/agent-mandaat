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
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mandaat.config import Instellingen
from mandaat.dpop import DPoPSleutel, token_hash
from mandaat.server.opslag import Opslag
from mandaat.server.poort import Agentpoort
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
    def __init__(self, status: int, inhoud: Any) -> None:
        self.status_code = status
        self._inhoud = inhoud

    def json(self) -> Any:
        return self._inhoud

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx2.HTTPError(f"status {self.status_code}")


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
        "typ": "DPoP",
        "azp": "dossierassistent",
        "scope": "dossier:lezen",
        "act": {"sub": "sub-agent", "client_id": "dossierassistent"},
        "cnf": {"jkt": DPOP.jkt},
    }
    claims.update(anders)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, SLEUTEL, "RS256")


def agent(
    controle: Tokencontrole,
    t: str,
    sleutel: DPoPSleutel = DPOP,
    ontvangen: str = INST.resource_url,
) -> dict[str, Any]:
    bewijs = sleutel.bewijs("POST", INST.resource_url, access_token=t)
    return controle.agenttoken(f"DPoP {t}", bewijs, "POST", ontvangen).claims


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
        ({"cnf": "geen-object"}, "DPoP-gebonden"),
        ({"cnf": {"jkt": 42}}, "DPoP-gebonden"),
        ({"jti": None}, "jti"),
        ({"typ": "Bearer"}, "tokentype"),
        ({"typ": "ID"}, "tokentype"),
        ({"typ": None}, "tokentype"),
        ({"act": "dossierassistent"}, "act"),
        ({"act": {"client_id": ["dossierassistent"]}}, "act"),
    ],
)
def test_ongeldig_agenttoken(controle: Tokencontrole, anders: dict[str, Any], melding: str) -> None:
    with pytest.raises(TokenFout, match=melding):
        agent(controle, token(**anders))


def test_bearer_schema_voor_tools_geweigerd(controle: Tokencontrole) -> None:
    with pytest.raises(TokenFout, match="DPoP"):
        controle.agenttoken(f"Bearer {token()}", None, "POST", INST.resource_url)


def test_bewijs_voor_andere_uri_dan_het_ontvangen_verzoek(controle: Tokencontrole) -> None:
    with pytest.raises(TokenFout, match="htu") as fout:
        agent(controle, token(), ontvangen="http://elders:8000/mcp")
    assert fout.value.code == "invalid_dpop_proof"


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


@pytest.mark.parametrize(
    "antwoord",
    [_Antwoord(500, {"active": True}), _Antwoord(200, {"active": "true"}), _Antwoord(200, [])],
)
def test_afwijkende_introspectie_faalt_dicht(
    controle: Tokencontrole, monkeypatch: pytest.MonkeyPatch, antwoord: _Antwoord
) -> None:
    monkeypatch.setattr(httpx2, "post", lambda *a, **k: antwoord)
    with pytest.raises(TokenFout, match="niet meer actief"):
        agent(controle, token())


def _medewerker(**anders: Any) -> str:
    standaard = {
        "act": None,
        "cnf": None,
        "typ": "Bearer",
        "azp": "werkplek",
        "scope": "mandaat:beheren",
    }
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
        ({"typ": "ID"}, "tokentype", 401),
        ({"typ": "DPoP"}, "tokentype", 401),
    ],
)
def test_ongeldig_medewerkertoken(
    controle: Tokencontrole, anders: dict[str, Any], melding: str, status: int
) -> None:
    with pytest.raises(TokenFout, match=melding) as fout:
        controle.medewerkertoken(f"Bearer {_medewerker(**anders)}", "mandaat:beheren")
    assert fout.value.status == status


def _met_sessies(monkeypatch: pytest.MonkeyPatch, inhoud: Any, status: int = 200) -> None:
    monkeypatch.setattr(httpx2, "post", lambda *a, **k: _Antwoord(200, {"access_token": "t"}))
    monkeypatch.setattr(httpx2, "get", lambda *a, **k: _Antwoord(status, inhoud))


def test_actieve_sessies(controle: Tokencontrole, monkeypatch: pytest.MonkeyPatch) -> None:
    inhoud = [
        {"id": "s1", "clients": {"uuid-1": "werkplek", "uuid-2": "account-console"}},
        {"id": "s2", "clients": {}},
    ]
    _met_sessies(monkeypatch, inhoud)
    assert controle.actieve_sessies("sub-sanne") == {
        "s1": frozenset({"werkplek", "account-console"}),
        "s2": frozenset(),
    }


@pytest.mark.parametrize(
    ("inhoud", "status"),
    [
        ({"id": "s1"}, 200),
        ([["s1"]], 200),
        ([{"clients": {}}], 200),
        ([{"id": "", "clients": {}}], 200),
        ([{"id": 1, "clients": {}}], 200),
        ([{"id": "s1"}], 200),
        ([{"id": "s1", "clients": ["werkplek"]}], 200),
        ([{"id": "s1", "clients": {"uuid": 1}}], 200),
        ([], 403),
    ],
)
def test_afwijkende_sessielijst_faalt_dicht(
    controle: Tokencontrole, monkeypatch: pytest.MonkeyPatch, inhoud: Any, status: int
) -> None:
    _met_sessies(monkeypatch, inhoud, status)
    with pytest.raises(TokenFout, match="sessies niet op te vragen") as fout:
        controle.actieve_sessies("sub-sanne")
    assert fout.value.status == 503


def _poort(tmp_path: Path, controle: Tokencontrole) -> tuple[TestClient, Opslag]:
    """De echte Agentpoort met de echte tokencontrole voor één MCP-route."""

    async def mcp(_request: Request) -> JSONResponse:
        return JSONResponse({"doorgelaten": True})

    binnen = Starlette(routes=[Route("/mcp", mcp, methods=["POST"])])
    opslag = Opslag(tmp_path / "db", 2)
    meta = "http://dossier-mcp:8000/.well-known/oauth-protected-resource/mcp"
    poort = Agentpoort(binnen, lambda: controle, lambda: opslag, {}, meta)
    return TestClient(poort, base_url="http://dossier-mcp:8000"), opslag


def _bewijs_met_jwk(t: str, iat: Any = None, **jwk: Any) -> str:
    claims = {
        "htm": "POST",
        "htu": INST.resource_url,
        "iat": int(time.time()) if iat is None else iat,
        "jti": str(uuid.uuid4()),
        "ath": token_hash(t),
    }
    kop = {"typ": "dpop+jwt", "jwk": {**DPOP.publieke_jwk, **jwk}}
    return jwt.encode(claims, DPOP.sleutel_priv, algorithm="ES256", headers=kop)


def _mcp(client: TestClient, t: str, bewijs: str) -> httpx2.Response:
    koppen = {"authorization": f"DPoP {t}", "dpop": bewijs}
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=koppen)


def test_geldige_aanroep_door_de_echte_poort(controle: Tokencontrole, tmp_path: Path) -> None:
    client, _ = _poort(tmp_path, controle)
    t = token()
    assert _mcp(client, t, _bewijs_met_jwk(t)).status_code == 200


GOEDE_X = DPOP.publieke_jwk["x"]


@pytest.mark.parametrize("x", [None, [], {}, 123, "", "a" * 43, GOEDE_X[:-1], GOEDE_X + "A"])
def test_misvormde_dpop_sleutel_geeft_geweigerde_aanroep_met_audit(
    controle: Tokencontrole, tmp_path: Path, x: Any
) -> None:
    client, opslag = _poort(tmp_path, controle)
    t = token()
    antwoord = _mcp(client, t, _bewijs_met_jwk(t, x=x))
    assert antwoord.status_code == 401, antwoord.text
    assert 'error="invalid_dpop_proof"' in antwoord.headers["www-authenticate"]
    record = opslag.auditlog()[-1]
    assert (record["beslissing"], record["laag"]) == ("geweigerd", "api-poort")
    assert record["medewerker"] == "sub-sanne"


@pytest.mark.parametrize("iat", [10**400, -(10**400), True, 1.5e9, float("inf"), "nu"])
def test_dpop_iat_buiten_bereik_geeft_geweigerde_aanroep_met_audit(
    controle: Tokencontrole, tmp_path: Path, iat: Any
) -> None:
    client, opslag = _poort(tmp_path, controle)
    t = token()
    antwoord = _mcp(client, t, _bewijs_met_jwk(t, iat=iat))
    assert antwoord.status_code == 401, antwoord.text
    assert 'error="invalid_dpop_proof"' in antwoord.headers["www-authenticate"]
    record = opslag.auditlog()[-1]
    assert (record["beslissing"], record["medewerker"]) == ("geweigerd", "sub-sanne")


@pytest.mark.parametrize(
    ("anders", "medewerker"),
    [
        ({"exp": 10**400}, "sub-sanne"),
        ({"iat": -(10**400)}, "sub-sanne"),
        ({"nbf": -1}, "sub-sanne"),
        ({"iat": str(int(time.time()))}, "sub-sanne"),
        ({"exp": str(int(time.time()) + 60)}, "sub-sanne"),
        ({"iat": True}, "sub-sanne"),
        ({"sub": ["sub-sanne"]}, None),
        ({"jti": 7}, None),
    ],
)
def test_claim_van_verkeerd_type_geeft_geweigerde_aanroep_met_audit(
    controle: Tokencontrole, tmp_path: Path, anders: dict[str, Any], medewerker: str | None
) -> None:
    client, opslag = _poort(tmp_path, controle)
    t = token(**anders)
    antwoord = _mcp(client, t, _bewijs_met_jwk(t))
    assert antwoord.status_code == 401, antwoord.text
    assert antwoord.json()["error_description"].startswith("token ongeldig")
    record = opslag.auditlog()[-1]
    assert (record["beslissing"], record["medewerker"]) == ("geweigerd", medewerker)
