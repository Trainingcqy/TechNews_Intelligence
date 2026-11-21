"""In-process MCP server abstraction for NewsDB tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..config import BEIJING, load_config
from ..evidence import EvidenceRegistry
from ..retrieval.corpus import corpus_info
from ..tools.registry import TOOLS, ToolOutcome, ToolSpec, TurnState, commit_call, prepare_call, run_call
from ..types import Evidence


@dataclass(frozen=True)
class MCPResourceSpec:
    uri: str
    name: str
    description: str
    mime_type: str
    reader: Callable[[], str]


def _evidence_row(evidence: Evidence) -> dict[str, str]:
    if evidence.doc == "discussion":
        discussion = evidence.discussion
        return {"eid": evidence.eid, "url": discussion.url, "title": discussion.title,
                "source": "HackerNews 讨论", "published_at": ""}
    article = evidence.article
    return {"eid": evidence.eid, "url": article.url, "title": article.title, "source": article.publisher,
            "published_at": f"{article.published_at.astimezone(BEIJING):%Y-%m-%d}"}


class InProcessMCPServer:
    """Minimal MCP-compatible server abstraction for local adapter phase."""

    def __init__(self, server_name: str):
        self.server_name = str(server_name).strip()
        if not self.server_name:
            raise ValueError("server_name is required")
        self._tools: dict[str, ToolSpec] = {}
        self._resources: dict[str, MCPResourceSpec] = {}

    def register_tool(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool '{spec.name}' is already registered on server '{self.server_name}'")
        self._tools[spec.name] = spec

    def list_tools(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for tool_name in sorted(self._tools.keys()):
            spec = self._tools[tool_name]
            rows.append(
                {
                    "server_name": self.server_name,
                    "name": spec.name,
                    "qualified_name": f"mcp__{self.server_name}__{spec.name}",
                    "description": spec.description,
                    "input_schema": spec.args_model.model_json_schema(),
                }
            )
        return rows

    def register_resource(
        self,
        *,
        uri: str,
        name: str,
        description: str,
        mime_type: str,
        reader: Callable[[], str],
    ) -> None:
        resource_uri = str(uri).strip()
        if not resource_uri:
            raise ValueError("resource uri is required")
        if resource_uri in self._resources:
            raise ValueError(f"Resource '{resource_uri}' is already registered on server '{self.server_name}'")
        self._resources[resource_uri] = MCPResourceSpec(
            uri=resource_uri,
            name=str(name),
            description=str(description),
            mime_type=str(mime_type),
            reader=reader,
        )

    def list_resources(self) -> list[dict[str, Any]]:
        return [
            {
                "uri": spec.uri,
                "name": spec.name,
                "description": spec.description,
                "mimeType": spec.mime_type,
            }
            for _, spec in sorted(self._resources.items())
        ]

    def read_resource(self, uri: str) -> dict[str, Any]:
        spec = self._resources.get(str(uri).strip())
        if spec is None:
            raise KeyError(f"Unknown resource: {uri}")
        return {"uri": spec.uri, "mimeType": spec.mime_type, "text": str(spec.reader())}

    def call_tool(self, tool_name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        # Each call has its own registry: evidence ids are only valid inside one call's return value.
        registry = EvidenceRegistry()
        state = TurnState(user_urls=set(), allow_any_library_url=True)
        config = load_config()
        call = {"name": str(tool_name).strip(), "args": payload or {}, "id": ""}
        outcome = prepare_call(0, call, state=state, config=config)
        if not isinstance(outcome, ToolOutcome):
            result = run_call(outcome, registry=registry, state=state)
            outcome = commit_call(result, registry=registry, state=state, config=config)
        return {
            "status": "ok" if outcome.ok else "error",
            "text": outcome.gather_text,
            "evidence": [_evidence_row(evidence) for evidence in registry.items()],
        }


def build_newsdb_server(server_name: str = "newsdb") -> InProcessMCPServer:
    """Build local NewsDB MCP server with namespaced tool contracts."""

    server = InProcessMCPServer(server_name=server_name)
    for spec in TOOLS:
        server.register_tool(spec)
    server.register_resource(
        uri="news://corpus",
        name="新闻库范围",
        description="Corpus overview: article count, date range, publisher roles, main publishers, topic and kind vocabularies (read-only snapshot).",
        mime_type="text/plain",
        reader=corpus_info,
    )
    return server
