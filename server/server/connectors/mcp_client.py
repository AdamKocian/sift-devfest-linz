"""Remote Dynatrace MCP server client: tools/list (discovery), tools/call (verification:
new Davis problems through execute_dql). The agent uses the same server through ADK.

Endpoint (docs): https://{environment}.apps.dynatrace.com/platform-reserved/mcp-gateway/v0.1/servers/dynatrace-mcp/mcp
Auth: `Authorization: Bearer <platform token>`; the token needs mcp-gateway:servers:invoke and
mcp-gateway:servers:read plus the scopes of the tools it calls (e.g. storage:logs:read).
https://docs.dynatrace.com/docs/shortlink/dynatrace-mcp-server
The remote tool list is not published; it comes from `tools/list`. The local
dynatrace-oss/dynatrace-mcp server was archived on 2026-09-11.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from server.connectors.base import ConnectorError, DynatraceEnv
from server.connectors.store import FixtureStore

MCP_PATH = "/platform-reserved/mcp-gateway/v0.1/servers/dynatrace-mcp/mcp"


@dataclass(frozen=True)
class McpTool:
    name: str
    description: str
    input_schema: dict[str, Any]


class DynatraceMcp(Protocol):
    def list_tools(self) -> list[McpTool]: ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


def parse_tools(body: dict[str, Any]) -> list[McpTool]:
    return [
        McpTool(t["name"], t.get("description") or "", t.get("inputSchema") or {})
        for t in body.get("tools", [])
    ]


def query_argument(tool: McpTool) -> str:
    """The argument of a DQL tool that takes the query text. Tool schemas come from
    `tools/list`; the remote server's are not published, so this reads the schema instead
    of assuming a name: the first required string property (else the first string one)."""
    props = tool.input_schema.get("properties") or {}
    strings = [k for k, v in props.items() if (v or {}).get("type") == "string"]
    required = [k for k in tool.input_schema.get("required") or [] if k in strings]
    if not (required or strings):
        raise ConnectorError(f"MCP tool {tool.name} has no string argument for the query")
    return (required or strings)[0]


def result_records(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Records from an execute_dql CallToolResult: `structuredContent.records` when present,
    else the first text block parsed as JSON (a list, or an object with `records`).
    The remote server's output format is not documented: TODO(verify) on a tenant."""
    import json

    if result.get("isError"):
        text = " ".join(c.get("text", "") for c in result.get("content") or [])
        raise ConnectorError(f"MCP tool error: {text[:300]}")
    sc = result.get("structuredContent")
    if isinstance(sc, dict) and isinstance(sc.get("records"), list):
        return sc["records"]
    for block in result.get("content") or []:
        if block.get("type") != "text":
            continue
        try:
            data = json.loads(block["text"])
        except (ValueError, KeyError):
            continue
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("records"), list):
            return data["records"]
    raise ConnectorError("could not read records from the MCP tool result")


def mcp_url(env: DynatraceEnv) -> str:
    return env.mcp_url or env.platform_url.rstrip("/") + MCP_PATH


class LiveDynatraceMcp:
    def __init__(self, env: DynatraceEnv) -> None:
        self.env = env
        self.raw: dict[str, Any] | None = None

    def list_tools(self) -> list[McpTool]:
        try:
            self.raw = asyncio.run(self._list())
        except Exception as exc:  # noqa: BLE001  (transport errors come in many types)
            raise ConnectorError(f"MCP tools/list failed: {exc}") from exc
        return parse_tools(self.raw)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return asyncio.run(self._session(lambda s: s.call_tool(name, arguments)))
        except Exception as exc:  # noqa: BLE001  (transport errors come in many types)
            raise ConnectorError(f"MCP call {name} failed: {exc}") from exc

    async def _list(self) -> dict[str, Any]:
        return await self._session(lambda s: s.list_tools())

    async def _session(self, op: Any) -> dict[str, Any]:
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        http = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {self.env.token}"}, timeout=self.env.timeout_s
        )
        async with http, streamable_http_client(mcp_url(self.env), http_client=http) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await op(session)
                return result.model_dump(mode="json", by_alias=True, exclude_none=True)


class FixtureDynatraceMcp:
    def __init__(self, store: FixtureStore) -> None:
        self.store = store

    def list_tools(self) -> list[McpTool]:
        return parse_tools(self.store.read("mcp", "tools.json")["response"])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # Fixture verification replays whole recorded checks (fixtures/verify/), so no MCP
        # call is recorded on its own.
        raise ConnectorError("fixture MCP replays tools/list only")
