"""ASGI-poort voor het MCP-endpoint: tokencontrole per HTTP-verzoek.

Elk verzoek naar `/mcp` (ook `initialize` en `tools/list`) vraagt een geldig, actief,
DPoP-gebonden token van een toegelaten agent. Bij `tools/call` controleert de poort ook of het token
de scope van die tool heeft (`403 insufficient_scope`). De mandaatcontrole en het eenmalig gebruik
van het token gebeuren daarna in de tool zelf, in één transactie met de actie.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import anyio
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mandaat.server.opslag import Opslag
from mandaat.server.tokens import Tokencontrole, TokenFout

DPOP_ALGS = "ES256"


def challenge(schema: str, fout: TokenFout, metadata_url: str, scope: str | None = None) -> str:
    """Bouw een WWW-Authenticate-waarde (RFC 6750 §3, RFC 9449 §7.1, RFC 9728 §5.1)."""
    beschrijving = fout.reden.replace('"', "'")
    delen = [f'error="{fout.code}"', f'error_description="{beschrijving}"']
    if schema == "DPoP":
        delen.insert(0, f'algs="{DPOP_ALGS}"')
    if scope:
        delen.append(f'scope="{scope}"')
    delen.append(f'resource_metadata="{metadata_url}"')
    return f"{schema} " + ", ".join(delen)


async def _lees_body(receive: Receive) -> bytes:
    delen = []
    while True:
        bericht = await receive()
        delen.append(bericht.get("body", b""))
        if not bericht.get("more_body"):
            return b"".join(delen)


def _toolaanroep(body: bytes) -> tuple[str | None, str | None]:
    """Toolnaam en dossier uit een JSON-RPC `tools/call`, anders (None, None)."""
    try:
        verzoek = json.loads(body)
    except ValueError:
        return None, None
    if not isinstance(verzoek, dict) or verzoek.get("method") != "tools/call":
        return None, None
    params = verzoek.get("params") or {}
    dossier = (params.get("arguments") or {}).get("dossier")
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

    def _controleer(
        self, headers: dict[str, str], methode: str, tool: str | None
    ) -> dict[str, Any]:
        gecontroleerd = self.controle().agenttoken(
            headers.get("authorization", ""), headers.get("dpop"), methode
        )
        bewijs = gecontroleerd.bewijs
        if bewijs is None or not self.opslag().dpop_bewijs_eenmalig(
            bewijs.jkt, bewijs.jti, bewijs.iat + 120
        ):
            raise TokenFout("DPoP-bewijs is al eerder gebruikt", code="invalid_dpop_proof")
        if tool is not None:
            nodig = self.toolscopes.get(tool)
            if nodig is None or nodig not in gecontroleerd.scopes:
                raise TokenFout(
                    f"tool {tool} vereist scope {nodig}", code="insufficient_scope", status=403
                )
        return gecontroleerd.claims

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
                "agent": (fout.claims.get("act") or {}).get("client_id"),
                "token_jti": fout.claims.get("jti"),
            }
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Verwerk een ASGI-verzoek."""
        if scope["type"] != "http" or not scope["path"].startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        body = await _lees_body(receive)
        tool, dossier = _toolaanroep(body)
        try:
            claims = await anyio.to_thread.run_sync(
                self._controleer, headers, scope["method"], tool
            )
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
