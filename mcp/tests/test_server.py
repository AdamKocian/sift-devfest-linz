"""The Sift MCP server: tool list, descriptions, and no way to approve or apply.

Unit tests run the server in-process against a mocked Sift API. The end-to-end test starts
the real Sift API (fixture mode, temporary database) and talks to the MCP server over stdio
and streamable HTTP, like an MCP client does (scripts/demo_client.py)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client

from sift_mcp.server import SiftApi, build_server

EXPECTED = {
    "telemetry_waste_report",
    "list_proposals",
    "explain_proposal",
    "propose",
    "proposal_status",
}
PROPOSAL = {
    "id": "p-payment-app-debug-drop",
    "title": "Drop DEBUG logs from payment",
    "status": "awaiting_approval",
    "action": "drop",
    "kind": "log_source",
    "service": "payment",
    "signal_key": "app · DEBUG",
    "savings": {"eur_month": 828.0, "gib_month": 345.0},
    "confidence": "high",
}


def fake_api(calls: list[tuple[str, str, Any]]) -> SiftApi:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, body or dict(req.url.params)))
        if req.url.path == "/api/proposals":
            return httpx.Response(200, json={"items": [PROPOSAL], "counts": {"awaiting": 1}})
        if req.url.path == "/api/proposals/propose":
            return httpx.Response(
                200,
                json={
                    "created": True,
                    "applied": False,
                    "mode": "deterministic",
                    "message": "Created",
                    "proposal": PROPOSAL,
                },  # fmt: skip
            )
        if req.url.path == "/api/proposals/nope":
            return httpx.Response(404, json={"detail": "proposal nope not found"})
        return httpx.Response(500, json={"detail": "unexpected"})

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url="http://sift", transport=httpx.MockTransport(handler))

    return SiftApi("http://sift", "http://ui", factory)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_tools_are_exactly_the_five_and_none_can_approve() -> None:
    async def go() -> list[Any]:
        async with Client(build_server(fake_api([]))) as c:
            return (await c.list_tools()).tools

    tools = run(go())
    assert {t.name for t in tools} == EXPECTED
    for t in tools:
        assert len(t.description or "") > 120, f"{t.name}: write the description for an LLM"
        assert not any(w in t.name for w in ("approve", "apply", "revert"))
    propose = next(t for t in tools if t.name == "propose")
    assert "NOT applied" in (propose.description or "")
    readonly = {t.name for t in tools if t.annotations and t.annotations.read_only_hint}
    assert readonly == EXPECTED - {"propose"}


def test_list_and_propose_call_the_sift_api() -> None:
    calls: list[tuple[str, str, Any]] = []

    async def go() -> tuple[Any, Any, Any]:
        async with Client(build_server(fake_api(calls))) as c:
            a = await c.call_tool("list_proposals", {"status": "awaiting"})
            b = await c.call_tool("propose", {"instruction": "drop debug logs from payment"})
            e = await c.call_tool("explain_proposal", {"id": "nope"})
            return a, b, e

    listed, proposed, missing = run(go())
    assert listed.structured_content["items"][0]["url"] == (
        "http://ui/#/proposals/p-payment-app-debug-drop"
    )
    assert proposed.structured_content["applied"] is False
    assert calls[0] == ("GET", "/api/proposals", {"status": "awaiting"})
    assert calls[1] == (
        "POST",
        "/api/proposals/propose",
        {"instruction": "drop debug logs from payment"},
    )
    assert missing.is_error and "proposal nope not found" in missing.content[0].text


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to start the Sift API")
def test_end_to_end_stdio_and_http_report_the_planted_cases() -> None:
    spec = importlib.util.spec_from_file_location(
        "demo_client", Path(__file__).resolve().parents[1] / "scripts" / "demo_client.py"
    )
    assert spec and spec.loader
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    with demo.sift_api() as api:
        results = run(demo.run(api, ["stdio", "http"]))
    for transport in ("stdio", "http"):
        out = results[transport]
        assert {name for name, _ in out["tools"]} == EXPECTED
        r = out["report"]
        awaiting = {p["proposal_id"] for p in r["proposals"]["awaiting"]}
        assert awaiting == {
            "p-payment-app-debug-drop",
            "p-frontend-get-healthz-drop",
            "p-cart-app-warn-timeout-calling-inventory-65c2-drop",
            "p-email-app-info-sent-confirmation-to-1f3c-redact",
        }
        verified = {p["proposal_id"] for p in r["proposals"]["verified"]}
        assert verified == {"p-inventory-heartbeat", "p-http-duration-userid"}
        protected = {s["signal_id"] for s in r["protected"]}
        assert {"log-recommendation-app-error", "log-checkout-app-info"} <= protected
