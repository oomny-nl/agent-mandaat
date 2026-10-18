"""Agentkant zonder keten: discovery, werkplek-login en het onderscheid weigering of fout."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

import httpx2
import pytest
from mcp.client.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mandaat.agent.graaf import _soort
from mandaat.agent.makelaar import GedelegeerdToken, ontdek, well_known
from mandaat.agent.verbinding import AgentAuth
from mandaat.config import Instellingen
from mandaat.dpop import DPoPSleutel
from mandaat.werkplek.sessie import Werkplek, WerkplekFout

INST = Instellingen(
    keycloak_url="http://keycloak:8080",
    realm="lindevoort",
    resource_url="http://dossier-mcp:8000/mcp",
    geheimen_dir=Path("/onbestaand"),
    db_pad=Path("/onbestaand/db"),
    max_skew=2,
)


@pytest.mark.parametrize(
    ("url", "naam", "verwacht"),
    [
        (
            "http://keycloak:8080/realms/lindevoort",
            "oauth-authorization-server",
            "http://keycloak:8080/.well-known/oauth-authorization-server/realms/lindevoort",
        ),
        (
            "http://dossier-mcp:8000/mcp",
            "oauth-protected-resource",
            "http://dossier-mcp:8000/.well-known/oauth-protected-resource/mcp",
        ),
        (
            "https://as.example.com/",
            "oauth-authorization-server",
            "https://as.example.com/.well-known/oauth-authorization-server",
        ),
    ],
)
def test_well_known_volgens_rfc8414(url: str, naam: str, verwacht: str) -> None:
    assert well_known(url, naam) == verwacht


class _Antwoord:
    def __init__(self, inhoud: dict[str, Any]) -> None:
        self.inhoud = inhoud

    def raise_for_status(self) -> _Antwoord:
        return self

    def json(self) -> dict[str, Any]:
        return self.inhoud


def _metadata(resource: str, issuer: str) -> dict[str, dict[str, Any]]:
    return {
        "http://dossier-mcp:8000/.well-known/oauth-protected-resource/mcp": {
            "resource": resource,
            "authorization_servers": [INST.issuer],
            "scopes_supported": ["dossier:lezen"],
        },
        "http://keycloak:8080/.well-known/oauth-authorization-server/realms/lindevoort": {
            "issuer": issuer,
            "token_endpoint": f"{INST.oidc}/token",
        },
    }


def test_ontdek(monkeypatch: pytest.MonkeyPatch) -> None:
    documenten = _metadata(INST.resource_url, INST.issuer)
    monkeypatch.setattr(httpx2, "get", lambda url, **_k: _Antwoord(documenten[url]))
    meta = ontdek(INST.resource_url)
    assert (meta.issuer, meta.token_endpoint) == (INST.issuer, f"{INST.oidc}/token")


@pytest.mark.parametrize(
    ("resource", "issuer", "melding"),
    [
        ("http://elders:8000/mcp", INST.issuer, "metadata hoort bij"),
        (INST.resource_url, "http://nep/realms/lindevoort", "wijkt af"),
    ],
)
def test_ontdek_weigert_afwijkende_metadata(
    monkeypatch: pytest.MonkeyPatch, resource: str, issuer: str, melding: str
) -> None:
    documenten = _metadata(resource, issuer)
    monkeypatch.setattr(httpx2, "get", lambda url, **_k: _Antwoord(documenten[url]))
    with pytest.raises(ValueError, match=melding):
        ontdek(INST.resource_url)


def _callback(status: int = 302, **params: str) -> httpx2.Response:
    locatie = f"{INST.redirect_uri}?{urlencode(params)}"
    return httpx2.Response(status, headers={"location": locatie})


WERKPLEK = Werkplek(INST, "sanne", "geheim")


def test_autorisatiecode_met_juiste_state_en_iss() -> None:
    antwoord = _callback(code="abc", state="s1", iss=INST.issuer)
    assert WERKPLEK.autorisatiecode(antwoord, "s1") == "abc"


@pytest.mark.parametrize(
    ("antwoord", "melding"),
    [
        (_callback(code="abc", state="ander", iss=INST.issuer), "state"),
        (_callback(code="abc", iss=INST.issuer), "state"),
        (_callback(code="abc", state="s1", iss="http://nep/realms/lindevoort"), "iss"),
        (_callback(code="abc", state="s1"), "iss"),
        (_callback(error="access_denied", state="s1", iss=INST.issuer), "access_denied"),
        (_callback(state="s1", iss=INST.issuer), "zonder autorisatiecode"),
        (_callback(200, code="abc", state="s1", iss=INST.issuer), "HTTP 200"),
        (
            httpx2.Response(302, headers={"location": "http://localhost:8765/elders?code=a"}),
            "geen autorisatiecode",
        ),
    ],
)
def test_autorisatiecode_geweigerd(antwoord: httpx2.Response, melding: str) -> None:
    with pytest.raises(WerkplekFout, match=melding):
        WERKPLEK.autorisatiecode(antwoord, "s1")


def test_doorverwijzingslus_stopt() -> None:
    class _Lus:
        def get(self, url: str) -> httpx2.Response:
            verzoek = httpx2.Request("GET", url)
            return httpx2.Response(302, headers={"location": url}, request=verzoek)

    start = httpx2.Response(
        302,
        headers={"location": "http://keycloak:8080/x"},
        request=httpx2.Request("POST", "http://keycloak:8080/login"),
    )
    with pytest.raises(WerkplekFout, match="doorverwijzingen"):
        WERKPLEK._naar_callback(cast(Any, _Lus()), start)


@pytest.mark.parametrize(
    ("fout", "tekst", "soort"),
    [
        (False, "Notitie 1 toegevoegd", "resultaat"),
        (True, "GEWEIGERD door de dossier-API: mandaat_ingetrokken", "api_weigert"),
        (True, "FOUT bij de dossier-API (geen weigering): timeout", "api_fout"),
        (True, "Error executing tool dossier_lezen: '2026-9999'", "api_fout"),
        (
            True,
            "Error executing tool dossier_lezen: GEWEIGERD door de dossier-API: x",
            "api_weigert",
        ),
    ],
)
def test_weigering_en_fout_zijn_verschillend(fout: bool, tekst: str, soort: str) -> None:
    assert _soort(fout, tekst) == soort


def test_weigering_van_de_tool_zelf_via_de_echte_sdk() -> None:
    """De weigering zoals de MCP SDK haar aan de client geeft, met het eigen voorvoegsel."""
    server = MCPServer("proef")

    @server.tool()
    def dossier_lezen(dossier: str) -> str:
        raise ToolError(f"GEWEIGERD door de dossier-API: dossier_buiten_mandaat ({dossier})")

    async def roep() -> tuple[bool, str]:
        async with Client(server) as client:
            resultaat = await client.call_tool("dossier_lezen", {"dossier": "2026-0157"})
        tekst = "\n".join(getattr(d, "text", "") for d in resultaat.content)
        return bool(resultaat.is_error), tekst

    fout, tekst = asyncio.run(roep())
    assert tekst.startswith("Error executing tool dossier_lezen: ")
    assert _soort(fout, tekst) == "api_weigert"


def test_weigering_van_een_eerder_verzoek_blijft_niet_hangen() -> None:
    sleutel = DPoPSleutel.nieuw()

    class _Makelaar:
        dpop = sleutel

        def gedelegeerd(self, _subject: str, scope: str) -> GedelegeerdToken:
            return GedelegeerdToken("token", scope, "jti", 2**31)

    auth = AgentAuth(cast(Any, _Makelaar()), "subject")
    for status, verwacht in ((401, 'DPoP error="invalid_token"'), (200, None)):
        flow = auth.auth_flow(httpx2.Request("POST", INST.resource_url, json={"method": "ping"}))
        verzoek = next(flow)
        kop = {"www-authenticate": 'DPoP error="invalid_token"'} if status == 401 else {}
        with pytest.raises(StopIteration):
            flow.send(httpx2.Response(status, headers=kop, request=verzoek))
        assert auth.laatste_weigering == verwacht
