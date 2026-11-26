"""Remote Dynatrace MCP server client. Phase 02 only lists tools; the agent uses them in
phase 03.

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


def parse_tools(body: dict[str, Any]) -> list[McpTool]:
    return [
        McpTool(t["name"], t.get("description") or "", t.get("inputSchema") or {})
        for t in body.get("tools", [])
    ]


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

    async def _list(self) -> dict[str, Any]:
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
                result = await session.list_tools()
                return result.model_dump(mode="json", by_alias=True, exclude_none=True)


class FixtureDynatraceMcp:
    def __init__(self, store: FixtureStore) -> None:
        self.store = store

    def list_tools(self) -> list[McpTool]:
        return parse_tools(self.store.read("mcp", "tools.json")["response"])
