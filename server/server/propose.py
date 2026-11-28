"""`POST /api/proposals/propose`: turn one plain instruction into a proposal (used by the Sift
MCP server's `propose` tool). It never applies anything: the result waits for a person to
approve it in the UI.

- SIFT_AGENT=replay (fixture mode, offline): deterministic. The instruction is matched to one
  signal with the same keyword rules as Ask Sift, then the agent's own tools draft, validate
  and create the proposal, so every guardrail and check of a real agent run applies. Dry runs
  for signals without a recording are answered from the demo world (written under data/).
- SIFT_AGENT=gemini: the live agent gets the instruction (same tools). Not run yet: billing
  on the GCP project is disabled, so this path is pending."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlmodel import Session

from server import chat, engine, views
from server.agent.tools import SiftTools, ToolError
from server.config import Config
from server.models import Signal

EXPLANATION = [
    "{service} · {key} sends {records_day} records a day, {gib_day} a day [1].",
    "It was last read {last_used}; {queries_30d} ad-hoc queries in the last month [2].",
    "The dry run matches {matched} of {scope_records} {scope_label} a day ({match_pct}) [3].",
    "That saves about {save_gib_month} and {save_eur_month} a month.",
]
REDACT_EXPLANATION = [
    "{service} · {key} sends {records_day} records a day, {gib_day} a day [1].",
    "Some of them carry e-mail addresses in the message text [1].",
    "The rule masks the addresses and keeps every record, so readers keep working [3].",
]


def _action(msg: str, sig: Signal) -> str:
    if re.search(r"\b(redact|mask|pii|e-?mails?)\b", msg):
        return "redact"
    if re.search(r"\b(metric|count|aggregate)\b", msg) and sig.data_type == "logs":
        return "logs_to_metric"
    if sig.kind in ("metric_dimension", "span_attribute"):
        return "drop_field"
    return "drop"


def _finding(action: str, sig: Signal, w: engine.World) -> str:
    if action == "redact":
        return "pii_risk"
    if action == "drop_field":
        return "high_cardinality"
    return "unused" if engine.usage(w, sig).klass == "unused" else "noise"


def deterministic(
    session: Session, cfg: Config, instruction: str, now: datetime, grail: Any
) -> dict[str, Any]:
    msg = instruction.lower()
    w = engine.load_world(session, now)
    sig = chat._find_signal(w, msg, chat._service(msg), chat._level(msg))
    base: dict[str, Any] = {"instruction": instruction, "mode": "deterministic", "applied": False}
    if sig is None:
        return {
            **base,
            "created": False,
            "message": "No signal matches this instruction. Name a service and a log level "
            '("drop DEBUG logs from cart") or a known pattern (health checks, heartbeats).',
        }
    existing = next((p for p in w.proposals if p.signal_id == sig.id), None)
    if existing is not None:
        return {
            **base,
            "created": False,
            "signal_id": sig.id,
            "message": f"“{existing.title}” already exists for this signal ({existing.status}).",
            "proposal": views.proposal_summary(w, existing),
        }
    tools = SiftTools(session, grail, lambda: now, otelcol_bin=cfg.otelcol_bin)
    action = _action(msg, sig)
    try:
        d = tools.call("draft_rule", {"signal_id": sig.id, "action": action})
        if "error" in d:
            return {**base, "created": False, "signal_id": sig.id, "message": d["error"]}
        did = d["draft_id"]
        v = tools.call("validate_rule", {"draft_id": did})
        if "error" in v:
            return {**base, "created": False, "signal_id": sig.id, "message": v["error"]}
        res = tools.call(
            "create_proposal",
            {
                "draft_id": did,
                "finding_class": _finding(action, sig, w),
                "confidence": "medium",
                "explanation": REDACT_EXPLANATION if action == "redact" else EXPLANATION,
            },
        )
    except ToolError as e:
        return {**base, "created": False, "signal_id": sig.id, "message": str(e)}
    if not res.get("created"):
        reason = res.get("reason") or res.get("error") or "the proposal was not created"
        return {**base, "created": False, "signal_id": sig.id, "message": reason}
    w = engine.load_world(session, now)
    p = w.proposal(res["proposal_id"])
    assert p is not None
    return {
        **base,
        "created": True,
        "signal_id": sig.id,
        "message": f"Created “{p.title}”. It waits for approval in the Sift UI.",
        "proposal": views.proposal_summary(w, p),
    }
