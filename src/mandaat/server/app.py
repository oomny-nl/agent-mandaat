"""Dossier-API van de fictieve gemeente Lindevoort, ontsloten als MCP-server.

- MCP-tools voor de agent (`/mcp`), achter de Agentpoort (DPoP, introspectie, scope per tool).
- Mandaatregister voor de medewerker (`/mandaten`), met een eigen Bearer-token zonder `act`.
- Eigen besluit van de medewerker (`/dossiers/{dossier}/besluit`): mag de mens, nooit de agent.
- Protected Resource Metadata (RFC 9728) op `/.well-known/oauth-protected-resource/mcp`.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import anyio
import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mandaat.config import Instellingen
from mandaat.geheimen import Geheimen
from mandaat.server.gegevens import DOSSIERS, mag_zelf
from mandaat.server.opslag import HistorieGewijzigd, Opslag
from mandaat.server.poort import MAX_BODY, Agentpoort, TeGroot, challenge, lees_body
from mandaat.server.tokens import Gecontroleerd, Tokencontrole, TokenFout, actor

log = logging.getLogger("dossier-mcp")

TOOLSCOPES = {
    "dossier_lezen": "dossier:lezen",
    "notitie_toevoegen": "notitie:schrijven",
    "besluit_vastleggen": "besluit:nemen",
}
MAX_UITKOMST = 200


@dataclass
class Dienst:
    """Gedeelde onderdelen van de API: lui opgebouwd, en precies één keer.

    Ook bij gelijktijdige eerste verzoeken bestaat er maar één `Opslag` (één verbinding en één
    slot); twee instanties zouden elk hun eigen auditketen kunnen voortzetten.
    """

    inst: Instellingen
    _slot: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _opslag: Opslag | None = field(default=None, init=False, repr=False)
    _controle: Tokencontrole | None = field(default=None, init=False, repr=False)

    @property
    def opslag(self) -> Opslag:
        """Database van de API."""
        with self._slot:
            if self._opslag is None:
                self._opslag = Opslag(self.inst.db_pad, self.inst.max_skew, self.inst.anker_pad)
            return self._opslag

    @property
    def controle(self) -> Tokencontrole:
        """Tokencontrole tegen Keycloak."""
        with self._slot:
            if self._controle is None:
                geheim = Geheimen(self.inst.geheimen_dir).wachtwoord("resource-client")
                self._controle = Tokencontrole(self.inst, geheim)
            return self._controle

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
        "agent": actor(claims),
        "token_jti": claims.get("jti"),
        "dpop_jkt": (claims.get("cnf") or {}).get("jkt"),
        **velden,
    }


def _laat_toe(
    dienst: Dienst, claims: dict[str, Any], tool: str, dossier: str
) -> tuple[str, dict[str, Any]]:
    """Volledige toelating van een toolactie.

    Een weigering gaat direct het auditlog in. Bij toelating schrijft de tool het auditrecord
    samen met het resultaat, zodat er geen `toegestaan` staat voor een actie die mislukte.

    Returns:
        De gebruikersnaam van de medewerker en het auditrecord van de toelating.

    Raises:
        ToolError: Als de actie niet is toegestaan.
    """
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
    toegestaan = reden == "binnen_mandaat" and gebruiker is not None
    record = _auditrecord(
        claims,
        tool,
        dossier,
        beslissing="toegestaan" if toegestaan else "geweigerd",
        reden=reden,
        mandaat=mandaat,
        gebruiker=gebruiker,
    )
    if not toegestaan or gebruiker is None:
        dienst.opslag.audit(record)
        raise ToolError(f"GEWEIGERD door de dossier-API: {reden}")
    return gebruiker, record


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
        _, record = await anyio.to_thread.run_sync(
            _laat_toe, dienst, _claims(ctx), "dossier_lezen", dossier
        )
        inhoud = {**DOSSIERS[dossier], "notities": dienst.opslag.notities(dossier)}
        inhoud.pop("behandelaar", None)
        await anyio.to_thread.run_sync(dienst.opslag.audit, record)
        return json.dumps(inhoud, ensure_ascii=False, indent=2)

    @mcp.tool(
        meta={"scope": "notitie:schrijven"}, description="Zet een conceptnotitie in een dossier."
    )
    async def notitie_toevoegen(dossier: str, tekst: str, ctx: Context) -> str:
        claims = _claims(ctx)
        gebruiker, record = await anyio.to_thread.run_sync(
            _laat_toe, dienst, claims, "notitie_toevoegen", dossier
        )
        nr = await anyio.to_thread.run_sync(
            dienst.opslag.voeg_notitie_toe,
            dossier,
            tekst,
            gebruiker,
            claims["act"]["client_id"],
            record,
        )
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
            "agent": actor(fout.claims),
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
        dossier = request.path_params["dossier"]
        uitkomst, fout = await _lees_uitkomst(request)
        if uitkomst is None:
            return await anyio.to_thread.run_sync(_besluitfout, dienst, token.claims, dossier, fout)
        return await anyio.to_thread.run_sync(_besluit, dienst, token.claims, dossier, uitkomst)


async def _lees_uitkomst(request: Request) -> tuple[str | None, str]:
    """`uitkomst` uit de JSON-body (begrensd), of (None, reden) bij een ongeldig verzoek."""
    try:
        body = await lees_body(request.receive)
    except TeGroot:
        return None, f"verzoek groter dan {MAX_BODY} bytes"
    try:
        inhoud = json.loads(body)
    except ValueError:
        return None, "body is geen geldige JSON"
    uitkomst = inhoud.get("uitkomst") if isinstance(inhoud, dict) else None
    if not isinstance(uitkomst, str) or not uitkomst.strip():
        return None, "veld uitkomst ontbreekt of is geen tekst"
    if len(uitkomst) > MAX_UITKOMST:
        return None, f"uitkomst langer dan {MAX_UITKOMST} tekens"
    return uitkomst.strip(), ""


def _weiger_beheer(
    dienst: Dienst, claims: dict[str, Any], handeling: str, reden: str, status: int
) -> Response:
    """Weigering van een beheerverzoek, met auditrecord."""
    dienst.opslag.audit(
        _beheerrecord(claims, "geweigerd", tool=handeling, reden=reden, dossier=claims.get("taak"))
    )
    return JSONResponse({"error": reden}, status_code=status)


def _beheerrecord(claims: dict[str, Any], beslissing: str, **velden: Any) -> dict[str, Any]:
    return {
        "laag": "api-beheer",
        "beslissing": beslissing,
        "medewerker": claims.get("sub"),
        "gebruiker": claims.get("preferred_username"),
        **velden,
    }


def _trek_in(dienst: Dienst, claims: dict[str, Any], mandaat_id: str) -> Response:
    record = _beheerrecord(claims, "mandaat_ingetrokken")
    if not dienst.opslag.trek_in(mandaat_id, claims["sub"], audit=record):
        reden = "geen actief mandaat van deze medewerker"
        return _weiger_beheer(dienst, claims, f"mandaat intrekken {mandaat_id}", reden, 404)
    return JSONResponse({"mandaat": mandaat_id, "status": "ingetrokken"})


def _sessies_beeindigd(dienst: Dienst, sub: str, sid: str) -> tuple[set[str] | None, str, int]:
    """Sessies van eerdere mandaten als alleen de eigen werkpleksessie nog actief is.

    Het gedelegeerde token van Keycloak draagt geen `sid`: de API kan niet zien uit welke sessie het
    subject token kwam. Daarom moet bij registratie elke andere werkpleksessie van de medewerker
    beëindigd zijn, met of zonder eerder mandaat. Anders kan haar subject token tokens opleveren
    die onder dit nieuwe mandaat vallen.

    Returns:
        (eerdere sessies, "", 0), of (None, reden, status) als registratie niet mag.
    """
    eerder = dienst.opslag.eerdere_sessies(sub, dienst.inst.agent_id)
    try:
        actief = dienst.controle.actieve_sessies(sub)
    except TokenFout as fout:
        return None, fout.reden, fout.status
    if eerder & actief.keys():
        return None, "de sessie van een eerder mandaat is nog actief; log die eerst uit", 409
    if any(s != sid and dienst.inst.werkplek_id in c for s, c in actief.items()):
        return None, "er is nog een andere werkpleksessie van deze medewerker actief", 409
    return eerder, "", 0


def _verleen(dienst: Dienst, claims: dict[str, Any]) -> Response:
    gebruiker, taak, sid = claims.get("preferred_username"), claims.get("taak"), claims.get("sid")
    handeling = "mandaat verlenen"
    if not (gebruiker and taak and sid):
        return _weiger_beheer(
            dienst, claims, handeling, "token mist preferred_username, taak of sid", 400
        )
    reden = mag_zelf(gebruiker, "dossier:lezen", taak)
    if reden:
        return _weiger_beheer(dienst, claims, handeling, f"geen mandaat mogelijk: {reden}", 403)
    gegevens = {
        "medewerker": claims["sub"],
        "gebruiker": gebruiker,
        "naam": claims.get("name", ""),
        "agent": dienst.inst.agent_id,
        "dossier": taak,
        "sid": sid,
    }
    # Elke andere delegatiebron moet echt weg zijn: alleen de eigen werkpleksessie is nog actief.
    # De registratie controleert in haar transactie dat de historie sindsdien niet veranderde.
    for _ in range(3):
        eerder, reden, status = _sessies_beeindigd(dienst, claims["sub"], sid)
        if eerder is None:
            return _weiger_beheer(dienst, claims, handeling, reden, status)
        record = _beheerrecord(claims, "mandaat_verleend", agent=dienst.inst.agent_id, dossier=taak)
        try:
            mandaat = dienst.opslag.registreer_mandaat(gegevens, eerder, audit=record)
        except HistorieGewijzigd:
            continue
        except ValueError as fout:
            return _weiger_beheer(dienst, claims, handeling, str(fout), 409)
        return JSONResponse(mandaat, status_code=201)
    return _weiger_beheer(dienst, claims, handeling, "mandaathistorie bleef wijzigen", 409)


def _besluitrecord(claims: dict[str, Any], dossier: str, reden: str | None) -> dict[str, Any]:
    return {
        "laag": "api-medewerker",
        "tool": "besluit (eigen handeling)",
        "dossier": dossier,
        "medewerker": claims["sub"],
        "gebruiker": str(claims.get("preferred_username", "")),
        "agent": None,
        "beslissing": "geweigerd" if reden else "toegestaan",
        "reden": reden or "eigen_bevoegdheid",
    }


def _besluitfout(dienst: Dienst, claims: dict[str, Any], dossier: str, reden: str) -> Response:
    """Ongeldig besluitverzoek: 400, met auditrecord."""
    dienst.opslag.audit(_besluitrecord(claims, dossier, f"ongeldig verzoek: {reden}"))
    return JSONResponse({"error": "invalid_request", "error_description": reden}, status_code=400)


def _besluit(dienst: Dienst, claims: dict[str, Any], dossier: str, uitkomst: str) -> Response:
    gebruiker = str(claims.get("preferred_username", ""))
    reden = mag_zelf(gebruiker, "besluit:nemen", dossier)
    record = _besluitrecord(claims, dossier, reden)
    if reden:
        dienst.opslag.audit(record)
        return JSONResponse({"error": reden}, status_code=403)
    nr = dienst.opslag.leg_besluit_vast(dossier, uitkomst, gebruiker, audit=record)
    return JSONResponse(
        {"besluit": nr, "dossier": dossier, "uitkomst": uitkomst, "door": gebruiker}
    )


def transportbeveiliging(resource_url: str) -> TransportSecuritySettings:
    """Host- en Origin-controle van de MCP-transportlaag (DNS-rebinding).

    Alleen de host uit de canonieke resource-URI is toegestaan. Er is geen browserclient, dus een
    meegestuurde `Origin` is altijd ongeldig (403); clients zonder `Origin` gaan door.
    """
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[urlsplit(resource_url).netloc],
        allowed_origins=[],
    )


def maak_app(dienst: Dienst) -> Agentpoort:
    """De volledige ASGI-app: MCP plus routes, achter de Agentpoort."""
    mcp = maak_mcp(dienst)
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=transportbeveiliging(dienst.inst.resource_url),
    )
    return Agentpoort(
        app, lambda: dienst.controle, lambda: dienst.opslag, TOOLSCOPES, dienst.metadata_url
    )


def main() -> None:
    """Start de API (in de container op poort 8000)."""
    logging.basicConfig(level=logging.INFO)
    dienst = Dienst(Instellingen.uit_omgeving())
    uvicorn.run(maak_app(dienst), host="0.0.0.0", port=8000, log_level="info")  # noqa: S104
