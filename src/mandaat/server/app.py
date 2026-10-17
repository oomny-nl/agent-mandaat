"""Dossier-API van de fictieve gemeente Lindevoort, ontsloten als MCP-server.

- MCP-tools voor de agent (`/mcp`), achter de Agentpoort (DPoP, introspectie, scope per tool).
- Mandaatregister voor de medewerker (`/mandaten`), met een eigen Bearer-token zonder `act`.
- Eigen besluit van de medewerker (`/dossiers/{dossier}/besluit`): mag de mens, nooit de agent.
- Protected Resource Metadata (RFC 9728) op `/.well-known/oauth-protected-resource/mcp`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import cached_property
from typing import Any

import anyio
import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mandaat.config import Instellingen
from mandaat.geheimen import Geheimen
from mandaat.server.gegevens import DOSSIERS, mag_zelf
from mandaat.server.opslag import Opslag
from mandaat.server.poort import Agentpoort, challenge
from mandaat.server.tokens import Gecontroleerd, Tokencontrole, TokenFout

log = logging.getLogger("dossier-mcp")

TOOLSCOPES = {
    "dossier_lezen": "dossier:lezen",
    "notitie_toevoegen": "notitie:schrijven",
    "besluit_vastleggen": "besluit:nemen",
}


@dataclass
class Dienst:
    """Gedeelde onderdelen van de API (lui opgebouwd, zodat tests ze kunnen vervangen)."""

    inst: Instellingen

    @cached_property
    def opslag(self) -> Opslag:
        """Database van de API."""
        return Opslag(self.inst.db_pad, self.inst.max_skew, self.inst.anker_pad)

    @cached_property
    def controle(self) -> Tokencontrole:
        """Tokencontrole tegen Keycloak."""
        return Tokencontrole(
            self.inst, Geheimen(self.inst.geheimen_dir).wachtwoord("resource-client")
        )

    @property
    def metadata_url(self) -> str:
        """URL van de Protected Resource Metadata."""
        basis, pad = self.inst.resource_url.rsplit("/", 1)
        return f"{basis}/.well-known/oauth-protected-resource/{pad}"

    def protected_resource_metadata(self) -> dict[str, Any]:
        """RFC 9728-document voor deze resource."""
        return {
            "resource": self.inst.resource_url,
            "resource_name": "Dossier-API gemeente Lindevoort (fictief)",
            "authorization_servers": [self.inst.issuer],
            "scopes_supported": sorted(set(TOOLSCOPES.values())),
            "bearer_methods_supported": ["header"],
            "dpop_signing_alg_values_supported": ["ES256"],
            "dpop_bound_access_tokens_required": True,
        }


def _auditrecord(claims: dict[str, Any], tool: str, dossier: str, **velden: Any) -> dict[str, Any]:
    return {
        "laag": "api-mandaat",
        "tool": tool,
        "scope": TOOLSCOPES[tool],
        "dossier": dossier,
        "medewerker": claims.get("sub"),
        "agent": (claims.get("act") or {}).get("client_id"),
        "token_jti": claims.get("jti"),
        "dpop_jkt": (claims.get("cnf") or {}).get("jkt"),
        **velden,
    }


def _laat_toe(dienst: Dienst, claims: dict[str, Any], tool: str, dossier: str) -> str:
    """Volledige toelating van een toolactie; geeft de gebruikersnaam van de medewerker."""
    scope = TOOLSCOPES[tool]
    if scope == "besluit:nemen":
        reden = "gedelegeerde_partij_legt_geen_besluit_vast"
        mandaat = dienst.opslag.actief_mandaat(claims["sub"], claims["act"]["client_id"])
        gebruiker = dienst.opslag.gebruiker_voor_sub(claims["sub"])
    else:
        toelating = dienst.opslag.laat_toe(claims, dossier)
        reden, mandaat = toelating.reden, toelating.mandaat_id
        gebruiker = dienst.opslag.gebruiker_van(mandaat) if mandaat else None
        if toelating.toegestaan and gebruiker is not None:
            reden = mag_zelf(gebruiker, scope, dossier) or reden
    toegestaan = reden == "binnen_mandaat"
    dienst.opslag.audit(
        _auditrecord(
            claims,
            tool,
            dossier,
            beslissing="toegestaan" if toegestaan else "geweigerd",
            reden=reden,
            mandaat=mandaat,
            gebruiker=gebruiker,
        )
    )
    if not toegestaan or gebruiker is None:
        raise ToolError(f"GEWEIGERD door de dossier-API: {reden}")
    return gebruiker


def _claims(ctx: Context) -> dict[str, Any]:
    request = ctx.request_context.request
    if request is None:
        raise ToolError(
            "geen HTTP-verzoek bij deze toolaanroep; alleen streamable HTTP is toegestaan"
        )
    return request.scope["state"]["agentclaims"]


def maak_mcp(dienst: Dienst) -> MCPServer:
    """Bouw de MCP-server met tools en beheerroutes."""
    mcp = MCPServer(
        "dossier-lindevoort",
        instructions="Dossiers van de fictieve gemeente Lindevoort; elke tool heeft een scope.",
    )

    @mcp.tool(
        meta={"scope": "dossier:lezen"},
        description="Lees een dossier (aanvraag, stukken, bijlagen).",
    )
    async def dossier_lezen(dossier: str, ctx: Context) -> str:
        await anyio.to_thread.run_sync(_laat_toe, dienst, _claims(ctx), "dossier_lezen", dossier)
        inhoud = {**DOSSIERS[dossier], "notities": dienst.opslag.notities(dossier)}
        inhoud.pop("behandelaar", None)
        return json.dumps(inhoud, ensure_ascii=False, indent=2)

    @mcp.tool(
        meta={"scope": "notitie:schrijven"}, description="Zet een conceptnotitie in een dossier."
    )
    async def notitie_toevoegen(dossier: str, tekst: str, ctx: Context) -> str:
        claims = _claims(ctx)
        gebruiker = await anyio.to_thread.run_sync(
            _laat_toe, dienst, claims, "notitie_toevoegen", dossier
        )
        nr = dienst.opslag.voeg_notitie_toe(dossier, tekst, gebruiker, claims["act"]["client_id"])
        return f"Notitie {nr} toegevoegd aan {dossier} (concept, namens {gebruiker})"

    @mcp.tool(meta={"scope": "besluit:nemen"}, description="Leg een besluit vast op een aanvraag.")
    async def besluit_vastleggen(dossier: str, uitkomst: str, ctx: Context) -> str:
        await anyio.to_thread.run_sync(
            _laat_toe, dienst, _claims(ctx), "besluit_vastleggen", dossier
        )
        return "onbereikbaar: een agent legt nooit een besluit vast"

    _routes(mcp, dienst)
    return mcp


def _bearerfout(dienst: Dienst, fout: TokenFout, handeling: str) -> Response:
    dienst.opslag.audit(
        {
            "laag": "api-beheer",
            "beslissing": "geweigerd",
            "tool": handeling,
            "reden": fout.reden,
            "fout": fout.code,
            "medewerker": fout.claims.get("sub"),
            "gebruiker": fout.claims.get("preferred_username"),
            "agent": (fout.claims.get("act") or {}).get("client_id"),
        }
    )
    return JSONResponse(
        {"error": fout.code, "error_description": fout.reden},
        status_code=fout.status,
        headers={"WWW-Authenticate": challenge("Bearer", fout, dienst.metadata_url)},
    )


def _medewerker(dienst: Dienst, request: Request, scope: str) -> Gecontroleerd:
    return dienst.controle.medewerkertoken(request.headers.get("authorization", ""), scope)


def _routes(mcp: MCPServer, dienst: Dienst) -> None:
    @mcp.custom_route("/.well-known/oauth-protected-resource/mcp", methods=["GET"])
    async def metadata(_: Request) -> Response:
        return JSONResponse(dienst.protected_resource_metadata())

    @mcp.custom_route("/health", methods=["GET"])
    async def gezondheid(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    _beheerroutes(mcp, dienst)


def _beheerroutes(mcp: MCPServer, dienst: Dienst) -> None:
    """Routes voor de medewerker zelf (Bearer-token zonder `act`)."""

    @mcp.custom_route("/mandaten", methods=["POST"])
    async def verleen(request: Request) -> Response:
        try:
            token = await anyio.to_thread.run_sync(_medewerker, dienst, request, "mandaat:beheren")
        except TokenFout as fout:
            return _bearerfout(dienst, fout, "mandaat verlenen")
        return await anyio.to_thread.run_sync(_verleen, dienst, token.claims)

    @mcp.custom_route("/mandaten/{mandaat_id}", methods=["DELETE"])
    async def trek_in(request: Request) -> Response:
        try:
            token = await anyio.to_thread.run_sync(_medewerker, dienst, request, "mandaat:beheren")
        except TokenFout as fout:
            return _bearerfout(dienst, fout, "mandaat intrekken")
        mandaat_id = request.path_params["mandaat_id"]
        return await anyio.to_thread.run_sync(_trek_in, dienst, token.claims, mandaat_id)

    @mcp.custom_route("/dossiers/{dossier}/besluit", methods=["POST"])
    async def eigen_besluit(request: Request) -> Response:
        try:
            token = await anyio.to_thread.run_sync(_medewerker, dienst, request, "besluit:nemen")
        except TokenFout as fout:
            return _bearerfout(dienst, fout, "besluit (eigen handeling)")
        uitkomst = str((await request.json()).get("uitkomst", ""))
        return await anyio.to_thread.run_sync(
            _besluit, dienst, token.claims, request.path_params["dossier"], uitkomst
        )


def _trek_in(dienst: Dienst, claims: dict[str, Any], mandaat_id: str) -> Response:
    if not dienst.opslag.trek_in(mandaat_id, claims["sub"]):
        return JSONResponse({"error": "geen actief mandaat van deze medewerker"}, status_code=404)
    dienst.opslag.audit(
        {
            "laag": "api-beheer",
            "beslissing": "mandaat_ingetrokken",
            "mandaat": mandaat_id,
            "medewerker": claims["sub"],
            "gebruiker": claims.get("preferred_username"),
        }
    )
    return JSONResponse({"mandaat": mandaat_id, "status": "ingetrokken"})


def _verleen(dienst: Dienst, claims: dict[str, Any]) -> Response:
    gebruiker, taak, sid = claims.get("preferred_username"), claims.get("taak"), claims.get("sid")
    if not (gebruiker and taak and sid):
        return JSONResponse(
            {"error": "token mist preferred_username, taak of sid"}, status_code=400
        )
    reden = mag_zelf(gebruiker, "dossier:lezen", taak)
    if reden:
        return JSONResponse({"error": f"geen mandaat mogelijk: {reden}"}, status_code=403)
    # De delegatiebron van eerdere mandaten moet echt weg zijn: hun Keycloak-sessies zijn beëindigd.
    # Anders kan de agent met een oud subject token nieuwe tokens halen onder dit nieuwe mandaat.
    try:
        nog_actief = dienst.opslag.eerdere_sessies(
            claims["sub"], dienst.inst.agent_id
        ) & dienst.controle.actieve_sessies(claims["sub"])
    except TokenFout as fout:
        return JSONResponse({"error": fout.reden}, status_code=fout.status)
    if nog_actief:
        return JSONResponse(
            {"error": "de sessie van een eerder mandaat is nog actief; log die eerst uit"},
            status_code=409,
        )
    try:
        mandaat = dienst.opslag.registreer_mandaat(
            {
                "medewerker": claims["sub"],
                "gebruiker": gebruiker,
                "naam": claims.get("name", ""),
                "agent": dienst.inst.agent_id,
                "dossier": taak,
                "sid": sid,
            }
        )
    except ValueError as fout:
        return JSONResponse({"error": str(fout)}, status_code=409)
    dienst.opslag.audit(
        {
            "laag": "api-beheer",
            "beslissing": "mandaat_verleend",
            "mandaat": mandaat["id"],
            "medewerker": claims["sub"],
            "gebruiker": gebruiker,
            "agent": dienst.inst.agent_id,
            "dossier": taak,
        }
    )
    return JSONResponse(mandaat, status_code=201)


def _besluit(dienst: Dienst, claims: dict[str, Any], dossier: str, uitkomst: str) -> Response:
    gebruiker = str(claims.get("preferred_username", ""))
    reden = mag_zelf(gebruiker, "besluit:nemen", dossier)
    dienst.opslag.audit(
        {
            "laag": "api-medewerker",
            "tool": "besluit (eigen handeling)",
            "dossier": dossier,
            "medewerker": claims["sub"],
            "gebruiker": gebruiker,
            "agent": None,
            "beslissing": "geweigerd" if reden else "toegestaan",
            "reden": reden or "eigen_bevoegdheid",
        }
    )
    if reden:
        return JSONResponse({"error": reden}, status_code=403)
    nr = dienst.opslag.leg_besluit_vast(dossier, uitkomst, gebruiker)
    return JSONResponse(
        {"besluit": nr, "dossier": dossier, "uitkomst": uitkomst, "door": gebruiker}
    )


def maak_app(dienst: Dienst) -> Agentpoort:
    """De volledige ASGI-app: MCP plus routes, achter de Agentpoort."""
    mcp = maak_mcp(dienst)
    app = mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")  # noqa: S104
    return Agentpoort(
        app, lambda: dienst.controle, lambda: dienst.opslag, TOOLSCOPES, dienst.metadata_url
    )


def main() -> None:
    """Start de API (in de container op poort 8000)."""
    logging.basicConfig(level=logging.INFO)
    dienst = Dienst(Instellingen.uit_omgeving())
    uvicorn.run(maak_app(dienst), host="0.0.0.0", port=8000, log_level="info")  # noqa: S104
