"""MCP-verbinding van de agent met per verzoek het juiste token en een vers DPoP-bewijs.

Protocolverzoeken (`initialize`, `tools/list`) gebruiken een protocoltoken met de leesscope; dat
token wordt door de API niet verbruikt. Een `tools/call` gebruikt het token dat de agent vlak
ervoor speciaal voor die ene actie heeft opgehaald (`AgentAuth.volgende`); dat wordt bij gebruik
meteen weggegooid, zodat het nooit voor een tweede verzoek dient.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx2
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client

from mandaat.agent.makelaar import GedelegeerdToken, Makelaar


@dataclass
class AgentAuth(httpx2.Auth):
    """httpx2-authenticatie: zet `Authorization: DPoP` en een DPoP-bewijs op elk verzoek.

    Attributes:
        makelaar: Tokenmakelaar van de agent.
        subject_token: Subject token van de medewerker.
    """

    makelaar: Makelaar
    subject_token: str
    requires_request_body = True
    volgende: GedelegeerdToken | None = field(default=None, init=False)
    laatste_weigering: str | None = field(default=None, init=False)
    _protocol: GedelegeerdToken | None = field(default=None, init=False)

    def _protocoltoken(self) -> str:
        if self._protocol is None or self._protocol.verloopt < time.time() + 5:
            self._protocol = self.makelaar.gedelegeerd(self.subject_token, "dossier:lezen")
        return self._protocol.waarde

    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response]:
        """Voeg token en bewijs toe aan het verzoek."""
        methode = None
        if request.content:
            try:
                methode = json.loads(request.content).get("method")
            except (ValueError, AttributeError):
                methode = None
        if methode == "tools/call":
            gereed, self.volgende = self.volgende, None
            if gereed is None:
                raise RuntimeError("tools/call zonder vooraf opgehaald token voor deze actie")
            token = gereed.waarde
        else:
            token = self._protocoltoken()
        request.headers["Authorization"] = f"DPoP {token}"
        request.headers["DPoP"] = self.makelaar.dpop.bewijs(
            request.method, str(request.url), access_token=token
        )
        self.laatste_weigering = None
        antwoord = yield request
        if antwoord.status_code in (401, 403):
            self.laatste_weigering = antwoord.headers.get("www-authenticate", "")


@asynccontextmanager
async def verbind(
    makelaar: Makelaar, subject_token: str
) -> AsyncGenerator[tuple[Client, AgentAuth]]:
    """Open een MCP-sessie met de dossier-API namens de medewerker."""
    auth = AgentAuth(makelaar, subject_token)
    async with (
        httpx2.AsyncClient(auth=auth, timeout=30) as http,
        Client(streamable_http_client(makelaar.meta.resource, http_client=http)) as client,
    ):
        yield client, auth
