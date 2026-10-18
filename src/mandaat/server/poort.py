"""ASGI-poort voor het MCP-endpoint: tokencontrole per HTTP-verzoek.

Elk verzoek naar `/mcp` (ook `initialize` en `tools/list`) vraagt een geldig, actief,
DPoP-gebonden token van een toegelaten agent. Bij `tools/call` controleert de poort ook of het token
de scope van die tool heeft (`403 insufficient_scope`). De mandaatcontrole en het eenmalig gebruik
van het token gebeuren daarna in de tool zelf, in één transactie met de actie.

Het DPoP-bewijs wordt vergeleken met de URI van het ontvangen verzoek (schema, `Host` en pad), niet
met een vaste URL. Een verzoek mag maar één `Authorization`- en één `DPoP`-header hebben.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Callable
from typing import Any

import anyio
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mandaat.server.opslag import Opslag
from mandaat.server.tokens import Tokencontrole, TokenFout, actor

DPOP_ALGS = "ES256"


def _headertekst(tekst: str) -> str:
    """Tekst binnen de tekenset van `error_description` (RFC 6750 §3): ASCII zonder `"` en `\\`.

    Accenten vallen weg ("één" wordt "een"); de JSON-body houdt de volledige tekst.
    """
    ascii_tekst = unicodedata.normalize("NFKD", tekst).encode("ascii", "ignore").decode()
    return "".join(t for t in ascii_tekst.replace('"', "'") if " " <= t <= "~" and t != "\\")


def challenge(schema: str, fout: TokenFout, metadata_url: str, scope: str | None = None) -> str:
    """Bouw een WWW-Authenticate-waarde (RFC 6750 §3, RFC 9449 §7.1, RFC 9728 §5.1)."""
    beschrijving = _headertekst(fout.reden)
    delen = [f'error="{fout.code}"', f'error_description="{beschrijving}"']
    if schema == "DPoP":
        delen.insert(0, f'algs="{DPOP_ALGS}"')
    if scope:
        delen.append(f'scope="{scope}"')
    delen.append(f'resource_metadata="{metadata_url}"')
    return f"{schema} " + ", ".join(delen)


MAX_BODY = 64 * 1024


class TeGroot(Exception):
    """Verzoek groter dan MAX_BODY."""


async def lees_body(receive: Receive) -> bytes:
    """Lees de volledige body; `TeGroot` boven MAX_BODY."""
    delen: list[bytes] = []
    grootte = 0
    while True:
        bericht = await receive()
        deel = bericht.get("body", b"")
        grootte += len(deel)
        if grootte > MAX_BODY:
            raise TeGroot
        delen.append(deel)
        if not bericht.get("more_body"):
            return b"".join(delen)


ENKELVOUDIG = {
    "authorization": ("invalid_request", 400),
    "dpop": ("invalid_dpop_proof", 401),  # RFC 9449 §4.3 punt 1
}


def koppen(ruw: list[tuple[bytes, bytes]]) -> dict[str, str]:
    """ASGI-headers als dict, met een harde eis op enkelvoudige authenticatieheaders.

    Header-bytes worden als latin-1 gelezen (zoals HTTP ze definieert), zodat willekeurige bytes
    nooit tot een serverfout leiden; een ongeldig token of bewijs faalt daarna in de controle.

    Raises:
        TokenFout: Als `Authorization` of `DPoP` meer dan één keer voorkomt.
    """
    uitkomst: dict[str, str] = {}
    for naam_ruw, waarde_ruw in ruw:
        naam = naam_ruw.decode("latin-1").lower()
        if naam in uitkomst and naam in ENKELVOUDIG:
            code, status = ENKELVOUDIG[naam]
            raise TokenFout(f"header {naam} komt meer dan één keer voor", code, status)
        uitkomst[naam] = waarde_ruw.decode("latin-1")
    return uitkomst


def ontvangen_uri(scope: Scope, kop: dict[str, str]) -> str:
    """URI van het ontvangen verzoek zonder query: schema, `Host` en pad."""
    return f"{scope['scheme']}://{kop.get('host', '')}{scope['path']}"


def _toolaanroep(body: bytes) -> tuple[str | None, str | None]:
    """Toolnaam en dossier uit een JSON-RPC `tools/call`, anders (None, None)."""
    try:
        verzoek = json.loads(body)
    except ValueError:
        return None, None
    if not isinstance(verzoek, dict) or verzoek.get("method") != "tools/call":
        return None, None
    params = verzoek.get("params")
    if not isinstance(params, dict):
        return "(ongeldig)", None
    argumenten = params.get("arguments")
    dossier = argumenten.get("dossier") if isinstance(argumenten, dict) else None
    return str(params.get("name")), str(dossier) if dossier else None


class Agentpoort:
    """ASGI-middleware die `/mcp` afschermt.

    Args:
        app: De MCP-app.
        controle: Tokencontrole.
        opslag: Opslag voor eenmalige DPoP-bewijzen en audit.
        toolscopes: Toolnaam naar vereiste scope.
        metadata_url: URL van de Protected Resource Metadata.
    """

    def __init__(
        self,
        app: ASGIApp,
        controle: Callable[[], Tokencontrole],
        opslag: Callable[[], Opslag],
        toolscopes: dict[str, str],
        metadata_url: str,
    ) -> None:
        self.app = app
        self.controle = controle
        self.opslag = opslag
        self.toolscopes = toolscopes
        self.metadata_url = metadata_url

    async def _weiger(self, send: Send, fout: TokenFout, scope: str | None = None) -> None:
        kop = challenge("DPoP", fout, self.metadata_url, scope)
        body = json.dumps({"error": fout.code, "error_description": fout.reden}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": fout.status,
                "headers": [
                    (b"www-authenticate", kop.encode()),
                    (b"content-type", b"application/json"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _controleer(self, scope: Scope, tool: str | None) -> dict[str, Any]:
        kop = koppen(scope["headers"])
        gecontroleerd = self.controle().agenttoken(
            kop.get("authorization", ""),
            kop.get("dpop"),
            scope["method"],
            ontvangen_uri(scope, kop),
        )
        bewijs = gecontroleerd.bewijs
        claims = gecontroleerd.claims
        if bewijs is None or not self.opslag().dpop_bewijs_eenmalig(
            bewijs.jkt, bewijs.jti, bewijs.iat + 120
        ):
            raise TokenFout(
                "DPoP-bewijs is al eerder gebruikt", code="invalid_dpop_proof", claims=claims
            )
        if tool is not None:
            nodig = self.toolscopes.get(tool)
            if nodig is None or nodig not in gecontroleerd.scopes:
                raise TokenFout(
                    f"tool {tool} vereist scope {nodig}",
                    code="insufficient_scope",
                    status=403,
                    claims=claims,
                )
        return claims

    def _audit_weigering(self, fout: TokenFout, tool: str | None, dossier: str | None) -> None:
        sub = fout.claims.get("sub")
        self.opslag().audit(
            {
                "beslissing": "geweigerd",
                "laag": "api-poort",
                "tool": tool,
                "dossier": dossier,
                "reden": fout.reden,
                "fout": fout.code,
                "medewerker": sub,
                "gebruiker": self.opslag().gebruiker_voor_sub(sub) if sub else None,
                "agent": actor(fout.claims),
                "token_jti": fout.claims.get("jti"),
            }
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Verwerk een ASGI-verzoek."""
        if scope["type"] != "http" or not scope["path"].startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        try:
            body = await lees_body(receive)
        except TeGroot:
            record = {
                "beslissing": "geweigerd",
                "laag": "api-poort",
                "reden": f"verzoek groter dan {MAX_BODY} bytes",
                "fout": "te_groot",
            }
            await anyio.to_thread.run_sync(self.opslag().audit, record)
            await send({"type": "http.response.start", "status": 413, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        tool, dossier = _toolaanroep(body)
        try:
            claims = await anyio.to_thread.run_sync(self._controleer, scope, tool)
        except TokenFout as fout:
            await anyio.to_thread.run_sync(self._audit_weigering, fout, tool, dossier)
            nodig = self.toolscopes.get(tool or "") if fout.code == "insufficient_scope" else None
            await self._weiger(send, fout, nodig)
            return
        scope.setdefault("state", {})["agentclaims"] = claims
        verzonden = False

        async def herhaal() -> Message:
            nonlocal verzonden
            if verzonden:
                return await receive()
            verzonden = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, herhaal, send)
