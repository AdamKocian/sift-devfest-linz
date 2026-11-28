"""Sift MCP server: a thin layer over the Sift API (official MCP Python SDK, `MCPServer`,
which mcp 2 calls what mcp 1 called FastMCP).

Tools: telemetry_waste_report, list_proposals, explain_proposal, propose, proposal_status.
None of them can approve, apply or revert anything: approval stays with a person in the
Sift UI. `propose` only creates a proposal that waits for that approval.

Run:
  sift-mcp                         # stdio (for Claude Code, Gemini CLI, Cursor, Antigravity)
  sift-mcp --http --port 8766      # streamable HTTP on http://127.0.0.1:8766/mcp
Environment: SIFT_API_URL (default http://localhost:8000), SIFT_UI_URL (links in answers;
default: the API URL, which serves the web app after `make demo`)."""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

INSTRUCTIONS = """\
Sift finds Dynatrace telemetry that nobody uses (log sources, noisy log patterns, spans,
metric dimensions) and proposes ingest rules for it (OpenPipeline processors or
OpenTelemetry Collector config), with the evidence and the money.
Use telemetry_waste_report first for an overview, list_proposals and explain_proposal for
details, propose to turn a user's instruction into a new proposal. Every number in Sift's
answers is computed by code from Dynatrace data; quote them, do not recompute them.
You cannot approve, apply or revert a proposal: tell the user to review it in the Sift UI
(the `url` field). Costs are estimates at the user's own rates, not Dynatrace prices."""

STATUSES = ("awaiting", "applied", "verified", "reverted", "rejected")
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)


class SiftApi:
    def __init__(
        self,
        base_url: str,
        ui_url: str | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.ui_url = (ui_url or base_url).rstrip("/")
        self._factory = client_factory or (
            lambda: httpx.AsyncClient(base_url=self.base_url, timeout=60.0)
        )

    @classmethod
    def from_env(cls) -> SiftApi:
        api = os.environ.get("SIFT_API_URL", "http://localhost:8000")
        return cls(api, os.environ.get("SIFT_UI_URL") or None)

    def link(self, pid: str) -> str:
        return f"{self.ui_url}/#/proposals/{pid}"

    async def call(self, method: str, path: str, **kw: Any) -> Any:
        async with self._factory() as http:
            try:
                resp = await http.request(method, f"/api{path}", **kw)
            except httpx.HTTPError as e:
                raise ToolError(
                    f"Sift API at {self.base_url} is not reachable ({e}). Start it with "
                    "`make dev` or `make demo`, or set SIFT_API_URL."
                ) from e
        if resp.status_code >= 400:
            detail = resp.json().get("detail") if resp.content else resp.reason_phrase
            raise ToolError(f"Sift API {method} {path} -> {resp.status_code}: {detail}")
        return resp.json()


def _row(api: SiftApi, p: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": p["id"],
        "title": p["title"],
        "status": p["status"],
        "action": p["action"],
        "kind": p["kind"],
        "service": p["service"],
        "signal": p["signal_key"],
        "savings_eur_month": p["savings"]["eur_month"],
        "savings_gib_month": p["savings"]["gib_month"],
        "confidence": p["confidence"],
        "url": api.link(p["id"]),
    }


def build_server(api: SiftApi) -> MCPServer:
    mcp = MCPServer("sift", instructions=INSTRUCTIONS, version="0.1.0")

    @mcp.tool(annotations=READ_ONLY)
    async def telemetry_waste_report(window_days: int = 30) -> dict[str, Any]:
        """Report the Dynatrace telemetry that is ingested but not used, and what Sift does
        about it.

        Returns totals (ingest GiB/day, estimated cost €/month, savings awaiting approval and
        already verified), proposals grouped by status (awaiting, applied, verified,
        reverted, rejected) with savings, high-cost data Sift will not touch because a
        guardrail protects it (ERROR logs, audit logs, data an alert/SLO/workflow reads),
        and unused data without a proposal yet.

        Args:
            window_days: how far back "used" counts: a dashboard, notebook, alert or query
                that read the data in this many days makes it used (1-90, default 30;
                ad-hoc query counts cover 30 days).
        """
        if not 1 <= window_days <= 90:
            raise ToolError("window_days must be between 1 and 90")
        r = await api.call("GET", "/report/waste", params={"window_days": window_days})
        for items in r["proposals"].values():
            for p in items:
                p["url"] = api.link(p["proposal_id"])
        return r

    @mcp.tool(annotations=READ_ONLY)
    async def list_proposals(status: str = "awaiting") -> dict[str, Any]:
        """List Sift's proposals (ingest rules the agent wants to apply), largest saving first.

        Args:
            status: awaiting (default: waiting for a person to approve), applied (applied or
                being verified), verified, reverted, rejected, or "all".
        """
        if status != "all" and status not in STATUSES:
            raise ToolError(f"status must be one of {', '.join(STATUSES)} or all")
        params = {} if status == "all" else {"status": status}
        r = await api.call("GET", "/proposals", params=params)
        return {
            "status": status,
            "counts": r["counts"],
            "items": [_row(api, p) for p in r["items"]],
        }

    @mcp.tool(annotations=READ_ONLY)
    async def explain_proposal(id: str) -> dict[str, Any]:
        """Explain one proposal: the evidence (volume, usage, dry run, guardrails), the
        agent's explanation, the exact rule for OpenPipeline and the OpenTelemetry
        Collector, everything that references the data (blast radius), and its history.

        Args:
            id: the proposal id, e.g. "p-payment-app-debug-drop" (from list_proposals).
        """
        d = await api.call("GET", f"/proposals/{id}")
        return {
            **_row(api, d),
            "explanation": d["explanation"],
            "evidence": [{"label": e["label"], "text": e["text"]} for e in d["evidence"]],
            "dry_run": d["dry_run"],
            "blast_radius": d["blast_radius"],
            "blocked_by_guardrails": d["blocking_guardrails"],
            "rule_version": d["rule_version"],
            "rule": {
                "openpipeline": d["rule"].get("openpipeline", {}).get("code"),
                "otel_collector": d["rule"].get("otel", {}).get("code"),
            },
            "verification": d["verify_result"],
            "history": [
                {"at": h["at"], "actor": h["actor"], "summary": h["summary"]} for h in d["history"]
            ],
        }

    @mcp.tool()
    async def propose(instruction: str) -> dict[str, Any]:
        """Turn a plain instruction into a new Sift proposal, for example "drop DEBUG logs
        from cart but keep errors". Sift drafts and validates the rule with the same
        guardrails as its own agent (it refuses ERROR/FATAL logs, audit logs and data that
        alerts or dashboards read) and answers with the proposal or the reason it did not
        create one.

        The proposal is NOT applied: it waits for a person to approve it in the Sift UI
        (the returned url). This tool cannot approve or apply anything.

        Args:
            instruction: what to do, in plain words; name the service and the data.
        """
        r = await api.call("POST", "/proposals/propose", json={"instruction": instruction})
        if r.get("proposal"):
            r["proposal"] = _row(api, r["proposal"])
        return r

    @mcp.tool(annotations=READ_ONLY)
    async def proposal_status(id: str) -> dict[str, Any]:
        """Where a proposal stands: awaiting approval, applied, verifying (and when the
        verification window ends), verified, reverted (and why), or rejected; who decided,
        where the rule was applied, and the verification checks.

        Args:
            id: the proposal id, e.g. "p-payment-app-debug-drop".
        """
        d = await api.call("GET", f"/proposals/{id}")
        v = d.get("verify_result") or None
        return {
            "id": d["id"],
            "title": d["title"],
            "status": d["status"],
            "decided_by": d["decided_by"],
            "decided_at": d["decided_at"],
            "applied_at": d["applied_at"],
            "applied_target": d.get("applied_target"),
            "verify_due_at": d["verify_due_at"],
            "verification": None
            if v is None
            else {
                "passed": v["passed"],
                "checks": [
                    {"label": c["label"], "passed": c["passed"], "detail": c["detail"]}
                    for c in v["checks"]
                ],
                "incident": v.get("incident"),
            },
            "revert_reason": d["revert_reason"],
            "reject_reason": d["reject_reason"],
            "last_events": [h["summary"] for h in d["history"][-3:]],
            "url": api.link(d["id"]),
        }

    return mcp


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sift-mcp", description=__doc__.splitlines()[0])
    ap.add_argument("--http", action="store_true", help="streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8766)
    args = ap.parse_args(argv)
    server = build_server(SiftApi.from_env())
    if args.http:
        server.run("streamable-http", host=args.host, port=args.port)
    else:
        server.run("stdio")
    return 0
