"""De LLM-route: echte toolschema's gaan naar een OpenAI-compatibele endpoint (nagebootst)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from mandaat.agent.planner import llm_planner

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "dossier_lezen",
            "description": "Lees een dossier.",
            "parameters": {
                "type": "object",
                "properties": {"dossier": {"type": "string"}},
                "required": ["dossier"],
            },
        },
    }
]


ONTVANGEN: list[dict[str, Any]] = []


class _Model(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        lengte = int(self.headers["Content-Length"])
        ONTVANGEN.append(json.loads(self.rfile.read(lengte)))
        antwoord = {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 0,
            "model": "nep-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "dossier_lezen",
                                    "arguments": json.dumps({"dossier": "2026-0142"}),
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        data = json.dumps(antwoord).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Model)
    draad = threading.Thread(target=server.serve_forever, daemon=True)
    draad.start()
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "nep-sleutel-alleen-voor-test")
    monkeypatch.setenv("OPENAI_MODEL", "nep-model")
    yield "ok"
    server.shutdown()


def test_llm_planner_stuurt_echte_schemas_en_leest_tool_calls(model: str) -> None:
    ONTVANGEN.clear()
    antwoord = llm_planner(TOOLS)([HumanMessage("Vat dossier 2026-0142 samen.")])
    assert antwoord.tool_calls[0]["name"] == "dossier_lezen"
    assert antwoord.tool_calls[0]["args"] == {"dossier": "2026-0142"}
    verzoek = ONTVANGEN[0]
    assert verzoek["model"] == "nep-model"
    assert verzoek["tools"][0]["function"]["parameters"]["required"] == ["dossier"]
    assert verzoek["messages"][0]["role"] == "system"
