"""Ask Sift with the agent: the same tools and guardrails as an analysis run.

- SIFT_AGENT=gemini: live ADK agent on Vertex AI; tool calls stream as they happen.
- fixture mode: if fixtures/agent/chat.json holds a recorded answer to this message, its
  tool calls run again on the current data. The replay stops at the first result that
  differs from the recording (before anything is written), and the caller falls back to
  the scripted answers in server/chat.py."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Session

from server import engine, views
from server.agent import checker, prompts
from server.agent import runner as ar
from server.agent.gemini import GeminiClient
from server.agent.tools import SiftTools
from server.config import Config
from server.db import utcnow
from server.models import Proposal


def normalize(message: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", message.lower()))


def _context(message: str, proposal_id: str | None, signal_id: str | None) -> str:
    hints = []
    if proposal_id:
        hints.append(f"proposal {proposal_id}")
    if signal_id:
        hints.append(f"signal {signal_id}")
    return message if not hints else f"{message}\n\n(The user is looking at {', '.join(hints)}.)"


def _tool_event(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "tool",
        "tool": rec["name"],
        "label": rec["label"],
        "duration_ms": rec["duration_ms"],
        "rows": rec["rows"],
        "result": rec["result"],
    }


def _tokens(text: str) -> list[dict[str, Any]]:
    return [{"type": "token", "text": c} for c in re.findall(r"\S+\s*", text)]


def recorded_events(
    session: Session, cfg: Config, message: str, grail: Any
) -> list[dict[str, Any]] | None:
    rec = ar.load_recording(cfg.fixtures_dir / "agent" / "chat.json")
    if rec is None or normalize(rec.get("message", "")) != normalize(message):
        return None
    now = utcnow()
    created: list[Proposal] = []
    tools = SiftTools(
        session, grail, utcnow, on_proposal=created.append, user_message=message, world_time=now
    )
    for call in rec["calls"]:
        # Every earlier result matched, so a write here does what the recorded run did.
        result = tools.call(call["name"], call["args"])
        if not ar._same(result, call["result"]):
            return None  # the data changed since the recording: use the scripted answer
    final = rec.get("final_text") or ""
    if not final or checker.unbacked([final], tools.sources()):
        return None
    w = engine.load_world(session, now)
    out = [_tool_event(c) for c in tools.calls]
    out += _tokens(final)
    out += [{"type": "proposal", "proposal": views.proposal_summary(w, p)} for p in created]
    u = rec.get("usage", {})
    out.append({"type": "done", "scripted": True, "recorded": True, "usage": u})
    return out


async def live_events(
    db: Engine, cfg: Config, message: str, proposal_id: str | None, signal_id: str | None
) -> AsyncIterator[dict[str, Any]]:
    from server import runs

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    with Session(db, expire_on_commit=False) as session:
        now = utcnow()
        created: list[Proposal] = []

        def on_proposal(p: Proposal) -> None:
            created.append(p)
            w = engine.load_world(session, utcnow())
            queue.put_nowait({"type": "proposal", "proposal": views.proposal_summary(w, p)})

        tools = SiftTools(
            session,
            runs._grail(cfg, now, record_dry_runs=cfg.db_path.parent / "dry_runs"),
            utcnow,
            on_tool=lambda r: queue.put_nowait(_tool_event(r)),
            on_proposal=on_proposal,
            otelcol_bin=cfg.otelcol_bin,
            user_message=message,
            world_time=now,
        )
        gemini = GeminiClient.vertex(
            cfg.gcp_project, cfg.gcp_location, cfg.gemini_model, cfg.gemini_fast_model
        )
        mcp = (cfg.dt_mcp_url, cfg.dt_platform_token) if cfg.dt_mcp_url else None
        task = asyncio.create_task(
            ar.run_live(
                tools,
                gemini,
                model=cfg.gemini_model,
                instruction=prompts.CHAT,
                message=_context(message, proposal_id, signal_id),
                max_tokens=min(cfg.gemini_max_tokens_per_run, 200_000),
                max_llm_calls=min(cfg.gemini_max_llm_calls, 15),
                mcp=mcp,
                on_mcp=lambda r: queue.put_nowait(_tool_event(r)),
            )
        )
        while not task.done() or not queue.empty():
            try:
                yield await asyncio.wait_for(queue.get(), timeout=0.25)
            except TimeoutError:
                continue
        try:
            out = task.result()
        except Exception as exc:  # noqa: BLE001 (show the failure in the chat, not a 500)
            yield from_text(f"Gemini call failed: {type(exc).__name__}: {exc}")
            yield {"type": "done", "scripted": False}
            return
        text = out.final_text if out.final_text and not out.unbacked else out.summary
        for item in _tokens(text):
            yield item
        lg = out.ledger
        yield {
            "type": "done",
            "scripted": False,
            "usage": {
                "requested_model": lg.requested_model,
                "served_model": lg.served_model,
                "fallback_reason": lg.fallback_reason,
                "llm_calls": len(lg.calls),
                "tokens_in": lg.tokens_in,
                "tokens_out": lg.tokens_out,
                "cost_usd": lg.cost_usd,
            },
        }


def from_text(text: str) -> dict[str, Any]:
    return {"type": "token", "text": text}


def chat_recording_path(cfg: Config) -> Path:
    return cfg.fixtures_dir / "agent" / "chat.json"
