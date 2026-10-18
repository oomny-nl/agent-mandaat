"""Entra-variant: verzoeken volgens de agent-OBO-flow en claimnormalisatie (gemockt)."""

from __future__ import annotations

from typing import Any

import httpx2
import pytest

from mandaat.entra import (
    EntraAgent,
    EntraFout,
    blueprint_token,
    managed_identity_token,
    namens_medewerker,
    normaliseer_entra,
    normaliseer_keycloak,
)

AGENT = EntraAgent("tenant", "blueprint-app", "agent-app", "mi-client")


class _Antwoord:
    def __init__(self, status: int, inhoud: dict[str, Any]) -> None:
        self.status_code = status
        self._inhoud = inhoud
        self.text = str(inhoud)

    def json(self) -> dict[str, Any]:
        return self._inhoud


@pytest.fixture
def verzoeken(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    gezien: list[dict[str, Any]] = []

    def post(url: str, data: dict[str, str], **_: Any) -> _Antwoord:
        gezien.append({"url": url, **data})
        return _Antwoord(200, {"access_token": f"token-{len(gezien)}"})

    monkeypatch.setattr(httpx2, "post", post)
    return gezien


def test_blueprint_token_gebruikt_fmi_path_en_managed_identity(
    verzoeken: list[dict[str, Any]],
) -> None:
    assert blueprint_token(AGENT, "mi-token") == "token-1"
    verzoek = verzoeken[0]
    assert verzoek["client_id"] == "blueprint-app"
    assert verzoek["fmi_path"] == "agent-app"
    assert verzoek["client_assertion"] == "mi-token"
    assert verzoek["scope"] == "api://AzureADTokenExchange/.default"
    assert "client_secret" not in verzoek


def test_obo_door_agent_identity(verzoeken: list[dict[str, Any]]) -> None:
    namens_medewerker(AGENT, "t1", "gebruikerstoken", "api://dossier/dossier.lezen")
    verzoek = verzoeken[0]
    assert verzoek["client_id"] == "agent-app"
    assert verzoek["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert verzoek["requested_token_use"] == "on_behalf_of"
    assert verzoek["assertion"] == "gebruikerstoken"
    assert verzoek["client_assertion"] == "t1"


def test_weigering_wordt_fout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx2, "post", lambda *a, **k: _Antwoord(400, {"error": "AADSTS65001"}))
    with pytest.raises(EntraFout, match="AADSTS65001"):
        blueprint_token(AGENT, "mi-token")


def test_managed_identity_via_container_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    gezien: dict[str, Any] = {}

    def get(url: str, params: dict[str, str], headers: dict[str, str], **_: Any) -> _Antwoord:
        gezien.update(url=url, params=params, headers=headers)
        return _Antwoord(200, {"access_token": "mi"})

    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost:42356/msi/token")
    monkeypatch.setenv("IDENTITY_HEADER", "geheim-van-het-platform")
    monkeypatch.setattr(httpx2, "get", get)
    assert managed_identity_token("mi-client") == "mi"
    assert gezien["params"]["resource"] == "api://AzureADTokenExchange"
    assert gezien["headers"]["X-IDENTITY-HEADER"] == "geheim-van-het-platform"


# Claims per scenario volgens learn.microsoft.com/entra/agent-id/agent-token-claims.
OBO_NAMENS_MENS = {
    "idtyp": "user",
    "xms_idrel": "1",
    "azp": "agent",
    "scp": "dossier:lezen",
    "oid": "medewerker",
    "xms_act_fct": "3 11",
}


def test_normalisatie_keycloak_en_entra_zelfde_uitkomst() -> None:
    keycloak = normaliseer_keycloak(
        {"sub": "medewerker", "scope": "dossier:lezen", "act": {"client_id": "agent"}}
    )
    entra = normaliseer_entra(OBO_NAMENS_MENS, frozenset({"agent"}))
    assert keycloak == entra


def test_entra_v1_token_met_appid() -> None:
    claims = {k: v for k, v in OBO_NAMENS_MENS.items() if k != "azp"} | {"appid": "agent"}
    assert normaliseer_entra(claims, frozenset({"agent"})).agent == "agent"


@pytest.mark.parametrize(
    ("anders", "melding"),
    [
        ({"xms_act_fct": None}, "AgentIdentity"),
        ({"xms_act_fct": "3 9"}, "AgentIdentity"),
        # Autonoom (app-only): idtyp app, onderwerp is de agent identity, scp "/".
        (
            {"idtyp": "app", "xms_idrel": "7", "xms_sub_fct": "9 3 11", "scp": "/"},
            "app-only",
        ),
        ({"idtyp": None, "scp": "/"}, "app-only"),
        ({"scp": ""}, "app-only"),
        # Het eigen gebruikersaccount van de agent: idtyp user, maar xms_sub_fct 13.
        ({"xms_sub_fct": "13"}, "gebruikersaccount"),
        ({"azp": "vreemd"}, "niet toegelaten"),
        ({"oid": None}, "oid"),
    ],
)
def test_entra_weigert(anders: dict[str, Any], melding: str) -> None:
    claims = {k: v for k, v in (OBO_NAMENS_MENS | anders).items() if v is not None}
    with pytest.raises(ValueError, match=melding):
        normaliseer_entra(claims, frozenset({"agent"}))


def test_keycloak_zonder_act() -> None:
    with pytest.raises(ValueError, match="act"):
        normaliseer_keycloak({"sub": "m", "scope": "x"})
