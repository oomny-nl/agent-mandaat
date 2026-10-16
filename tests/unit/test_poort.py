"""De Agentpoort: challenges (RFC 9449 §7.1, RFC 9728 §5.1) en scope per tool."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mandaat.dpop import GevalideerdBewijs
from mandaat.server.opslag import Opslag
from mandaat.server.poort import Agentpoort
from mandaat.server.tokens import Gecontroleerd, TokenFout

META = "http://dossier-mcp:8000/.well-known/oauth-protected-resource/mcp"
SCOPES = {"dossier_lezen": "dossier:lezen", "besluit_vastleggen": "besluit:nemen"}


class _Controle:
    def __init__(self, fout: TokenFout | None = None) -> None:
        self.fout = fout

    def agenttoken(self, *_a: Any) -> Gecontroleerd:
        if self.fout:
            raise self.fout
        claims = {"sub": "s", "scope": "dossier:lezen", "act": {"client_id": "dossierassistent"}}
        return Gecontroleerd(claims, GevalideerdBewijs("jkt", str(uuid.uuid4()), int(time.time())))


def _app(tmp_path: Path, controle: _Controle) -> TestClient:
    async def mcp(request: Any) -> JSONResponse:
        return JSONResponse({"claims": request.scope["state"]["agentclaims"]})

    binnen = Starlette(routes=[Route("/mcp", mcp, methods=["POST"])])
    opslag = Opslag(tmp_path / "db", 2)
    poort = Agentpoort(binnen, lambda: cast(Any, controle), lambda: opslag, SCOPES, META)
    return TestClient(poort)


def _aanroep(tool: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": {"dossier": "2026-0142"}},
    }


def test_dpop_challenge_bij_ongeldig_token(tmp_path: Path) -> None:
    client = _app(tmp_path, _Controle(TokenFout("token is ingetrokken")))
    antwoord = client.post("/mcp", json=_aanroep("dossier_lezen"))
    assert antwoord.status_code == 401
    kop = antwoord.headers["www-authenticate"]
    assert kop.startswith('DPoP algs="ES256"')
    assert 'error="invalid_token"' in kop
    assert f'resource_metadata="{META}"' in kop


def test_ontbrekend_bewijs_geeft_invalid_dpop_proof(tmp_path: Path) -> None:
    client = _app(tmp_path, _Controle(TokenFout("bewijs ontbreekt", code="invalid_dpop_proof")))
    kop = client.post("/mcp", json=_aanroep("dossier_lezen")).headers["www-authenticate"]
    assert 'error="invalid_dpop_proof"' in kop


def test_scope_per_tool_geeft_403_met_scope(tmp_path: Path) -> None:
    antwoord = _app(tmp_path, _Controle()).post("/mcp", json=_aanroep("besluit_vastleggen"))
    assert antwoord.status_code == 403
    kop = antwoord.headers["www-authenticate"]
    assert 'error="insufficient_scope"' in kop
    assert 'scope="besluit:nemen"' in kop


def test_doorgelaten_verzoek_krijgt_claims_en_body(tmp_path: Path) -> None:
    antwoord = _app(tmp_path, _Controle()).post("/mcp", json=_aanroep("dossier_lezen"))
    assert antwoord.status_code == 200
    assert antwoord.json()["claims"]["act"]["client_id"] == "dossierassistent"


def test_hergebruikt_dpop_bewijs_geweigerd(tmp_path: Path) -> None:
    vast = GevalideerdBewijs("jkt", "zelfde-jti", int(time.time()))

    class _VastBewijs(_Controle):
        def agenttoken(self, *_a: Any) -> Gecontroleerd:
            claims = {
                "sub": "s",
                "scope": "dossier:lezen",
                "act": {"client_id": "dossierassistent"},
            }
            return Gecontroleerd(claims, vast)

    client = _app(tmp_path, _VastBewijs())
    assert client.post("/mcp", json=_aanroep("dossier_lezen")).status_code == 200
    tweede = client.post("/mcp", json=_aanroep("dossier_lezen"))
    assert tweede.status_code == 401
    assert 'error="invalid_dpop_proof"' in tweede.headers["www-authenticate"]


def test_weigering_staat_in_audit(tmp_path: Path) -> None:
    client = _app(tmp_path, _Controle(TokenFout("vervalst", claims={"sub": "s", "jti": "j"})))
    client.post("/mcp", content=json.dumps(_aanroep("dossier_lezen")))
    records = Opslag(tmp_path / "db", 2).auditlog()
    assert records[-1]["beslissing"] == "geweigerd"
    assert records[-1]["dossier"] == "2026-0142"
    assert records[-1]["laag"] == "api-poort"


@pytest.mark.parametrize("pad", ["/health", "/mandaten"])
def test_andere_paden_niet_afgeschermd(tmp_path: Path, pad: str) -> None:
    client = _app(tmp_path, _Controle(TokenFout("nooit")))
    assert client.post(pad).status_code == 404  # bestaat niet in deze test-app, wel doorgelaten
