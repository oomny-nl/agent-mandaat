"""De dossierassistent als LangGraph-graaf: planner en toolknoop in een lus.

De toolknoop haalt voor elke toolactie eerst een eigen gedelegeerd token op met precies de scope
van die tool (de scope staat in de toolmetadata van de MCP-server). Weigert Keycloak het token,
dan bereikt de aanroep de API niet eens. Weigert de API, dan komt de reden terug als toolfout.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from langchain_core.messages import AnyMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from mcp.client.client import Client
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, Field

from mandaat.agent.makelaar import GedelegeerdToken, Makelaar, MandaatGeweigerd
from mandaat.agent.planner import Planner
from mandaat.agent.verbinding import AgentAuth

Waarnemer = Callable[[dict[str, Any]], None]


class Toestand(BaseModel):
    """Toestand van de graaf: alleen de berichten."""

    berichten: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)


@dataclass
class Gereedschap:
    """Wat de toolknoop nodig heeft.

    Attributes:
        client: Open MCP-sessie.
        auth: Authenticatie van die sessie (krijgt per actie het token).
        makelaar: Tokenmakelaar.
        subject_token: Subject token van de medewerker.
        toolscopes: Toolnaam naar vereiste scope (uit de MCP-toolmetadata).
        waarnemer: Ontvangt gebeurtenissen voor de vertelling in de demo.
    """

    client: Client
    auth: AgentAuth
    makelaar: Makelaar
    subject_token: str
    toolscopes: dict[str, str]
    waarnemer: Waarnemer

    def token_voor(self, naam: str) -> GedelegeerdToken:
        """Haal een gedelegeerd token op voor één aanroep van deze tool."""
        return self.makelaar.gedelegeerd(self.subject_token, self.toolscopes.get(naam, ""))

    async def voer_uit(
        self,
        naam: str,
        argumenten: dict[str, Any],
        oproep_id: str = "handmatig",
        token: GedelegeerdToken | None = None,
    ) -> ToolMessage:
        """Voer één toolaanroep uit met een eigen token (of een vooraf opgehaald token)."""
        dossier = str(argumenten.get("dossier", ""))
        extra = {"dossier": dossier}
        scope = self.toolscopes.get(naam, "")
        self.waarnemer({"soort": "aanroep", "tool": naam, "scope": scope, "argumenten": argumenten})
        if token is not None:
            self.waarnemer({"soort": "vooraf", "tool": naam, "jti": token.jti})
        try:
            vers = token is None
            token = token or self.token_voor(naam)
        except MandaatGeweigerd as fout:
            self.waarnemer(
                {"soort": "keycloak_weigert", "tool": naam, "scope": scope, "fout": str(fout)}
            )
            return ToolMessage(
                f"GEWEIGERD door Keycloak: {fout}",
                tool_call_id=oproep_id,
                name=naam,
                status="error",
                additional_kwargs=extra,
            )
        if vers:
            self.waarnemer({"soort": "token", "tool": naam, "scope": token.scope, "jti": token.jti})
        self.auth.volgende = token
        try:
            resultaat = await self.client.call_tool(naam, argumenten)
        except MCPError:
            # De poort van de API weigerde het verzoek op HTTP-niveau (401 of 403).
            tekst, fout_ = (
                f"GEWEIGERD door de dossier-API: {_reden(self.auth.laatste_weigering)}",
                True,
            )
        else:
            tekst = "\n".join(getattr(d, "text", "") for d in resultaat.content)
            fout_ = bool(resultaat.is_error)
        self.waarnemer(
            {"soort": "api_weigert" if fout_ else "resultaat", "tool": naam, "tekst": tekst}
        )
        return ToolMessage(
            tekst,
            tool_call_id=oproep_id,
            name=naam,
            status="error" if fout_ else "success",
            additional_kwargs=extra,
        )


def _reden(challenge: str | None) -> str:
    """Haal error_description uit een WWW-Authenticate-waarde."""
    gevonden = re.search(r'error_description="([^"]*)"', challenge or "")
    return gevonden.group(1) if gevonden else "HTTP-weigering zonder toelichting"


def bouw_graaf(planner: Planner, gereedschap: Gereedschap) -> Any:
    """Bouw en compileer de graaf.

    Args:
        planner: Deterministische of LLM-planner.
        gereedschap: Toolknoop-afhankelijkheden.

    Returns:
        De gecompileerde LangGraph-graaf.
    """

    def plan(state: Toestand) -> dict[str, Any]:
        return {"berichten": [planner(state.berichten)]}

    async def gereedschapsknoop(state: Toestand) -> dict[str, Any]:
        laatste = state.berichten[-1]
        antwoorden = [
            await gereedschap.voer_uit(o["name"], o["args"], str(o["id"]))
            for o in getattr(laatste, "tool_calls", [])
        ]
        return {"berichten": antwoorden}

    def verder(state: Toestand) -> str:
        return "tools" if getattr(state.berichten[-1], "tool_calls", None) else END

    graaf = StateGraph(Toestand)
    graaf.add_node("planner", plan)
    graaf.add_node("tools", gereedschapsknoop)
    graaf.add_edge(START, "planner")
    graaf.add_conditional_edges("planner", verder, {"tools": "tools", END: END})
    graaf.add_edge("tools", "planner")
    return graaf.compile()


async def toolscopes(client: Client) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Lees tools en hun scopes uit de MCP-server; geeft ook het OpenAI-functieformaat."""
    lijst = await client.list_tools()
    scopes = {t.name: str((t.meta or {}).get("scope", "")) for t in lijst.tools}
    functies = [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.input_schema,
            },
        }
        for t in lijst.tools
    ]
    return scopes, functies


async def voer_opdracht_uit(graaf: Any, opdracht: str) -> list[AnyMessage]:
    """Laat de agent een opdracht uitvoeren en geef de berichten terug."""
    uitkomst = await graaf.ainvoke({"berichten": [HumanMessage(opdracht)]}, {"recursion_limit": 20})
    return list(uitkomst["berichten"])
