"""Talk to the Sift MCP server the way an MCP client does: tools/list, then
telemetry_waste_report, over stdio and over streamable HTTP.

    cd mcp
    uv run python scripts/demo_client.py --start-api     # starts its own offline Sift API
    uv run python scripts/demo_client.py                 # uses SIFT_API_URL (make dev / demo)

--start-api runs the Sift API in fixture mode on a temporary database and a free port, so it
does not touch the database of a running `make dev`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters

MCP_DIR = Path(__file__).resolve().parents[1]
SERVER_DIR = MCP_DIR.parent / "server"


def _wait_port(port: int, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.5):
            return
        time.sleep(0.2)
    raise TimeoutError(f"nothing listens on port {port}")


@contextlib.contextmanager
def sift_api(port: int | None = None) -> Iterator[str]:
    """The Sift API in fixture mode on a temporary database (a free port unless given)."""
    port = port or free_port()
    tmp = tempfile.mkdtemp(prefix="sift-mcp-demo-")
    env = {
        **os.environ,
        "SIFT_MODE": "fixture",
        "SIFT_AGENT": "replay",
        "SIFT_DB": f"{tmp}/sift.db",
        "SIFT_REPLAY_SPEED": "0",
        "SIFT_APPLY_TARGET": "dry-run",
    }
    proc = subprocess.Popen(
        ["uv", "run", "uvicorn", "server.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=SERVER_DIR,
        env=env,
    )
    try:
        _wait_port(port)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(10)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def mcp_http(api_url: str, port: int | None = None) -> Iterator[str]:
    """The Sift MCP server on streamable HTTP (a free port unless one is given)."""
    port = port or free_port()
    env = {**os.environ, "SIFT_API_URL": api_url}
    proc = subprocess.Popen(
        [sys.executable, "-m", "sift_mcp", "--http", "--port", str(port)],
        cwd=MCP_DIR,
        env=env,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_port(port)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        proc.wait(10)


def stdio_params(api_url: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "sift_mcp"],
        env={"SIFT_API_URL": api_url},
        cwd=str(MCP_DIR),
    )


async def session(server: Any, window_days: int = 30) -> dict[str, Any]:
    """tools/list + telemetry_waste_report through one MCP client connection."""
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        res = await client.call_tool("telemetry_waste_report", {"window_days": window_days})
        report = res.structured_content
        if report is None:  # unstructured fallback: the JSON text block
            report = json.loads(res.content[0].text)  # type: ignore[union-attr]
        return {"tools": [(t.name, (t.description or "").splitlines()[0]) for t in tools],
                "report": report, "is_error": res.is_error}  # fmt: skip


def print_session(transport: str, out: dict[str, Any]) -> None:
    print(f"\n== {transport}: tools/list ({len(out['tools'])} tools)")
    for name, line in out["tools"]:
        print(f"  {name:24} {line}")
    r = out["report"]
    t = r["totals"]
    print(
        f"== {transport}: telemetry_waste_report(window_days={r['window_days']}) on env "
        f"{r['environment']} ({r['mode']} mode)\n"
        f"  cost ~€{t['cost_eur_month']:,.0f}/month, awaiting approval "
        f"€{t['awaiting_savings_eur_month']:,.0f}/month, verified €"
        f"{t['verified_savings_eur_month']:,.0f}/month"
    )
    for group in ("awaiting", "verified", "reverted", "rejected"):
        for p in r["proposals"][group]:
            eur = f"€{p['savings_eur_month']:,.0f}/mo"
            print(f"  [{group:8}] {p['title']}  ({eur})  {p['proposal_id']}")
    for s in r["protected"]:
        print(f"  [protected] {s['service']} · {s['key']}  (€{s['eur_month']:,.0f}/mo): "
              f"{', '.join(s['guardrails'])}")  # fmt: skip


async def run(api_url: str, transports: list[str]) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    if "stdio" in transports:
        results["stdio"] = await session(stdio_params(api_url))
        print_session("stdio", results["stdio"])
    if "http" in transports:
        with mcp_http(api_url) as url:
            results["http"] = await session(url)
            print_session(f"streamable HTTP {url}", results["http"])
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start-api", action="store_true", help="start an offline Sift API")
    ap.add_argument("--transport", choices=["stdio", "http", "both"], default="both")
    args = ap.parse_args()
    transports = ["stdio", "http"] if args.transport == "both" else [args.transport]
    if args.start_api:
        with sift_api() as url:
            asyncio.run(run(url, transports))
    else:
        asyncio.run(run(os.environ.get("SIFT_API_URL", "http://localhost:8000"), transports))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
