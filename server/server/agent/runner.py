"""Agent runs: live on Gemini through ADK, and replayed from a recording.

Live: an ADK `LlmAgent` whose model is `SiftGemini` (the one Gemini client) and whose tools
are Sift's tools (and the remote Dynatrace MCP toolset when DT_MCP_URL is set). Every tool
call is written to the run log as it happens; the final text must pass the number checker
or the run summary falls back to a summary written by code.

Replay (fixture mode): a recording (fixtures/agent/*.json) lists the real run's tool calls
with their results and the agent's texts. The replay executes the same calls again through
the same tools on the current database. If every result matches the recording, the
agent's texts are replayed too; if the data has changed since (another state), the texts
are dropped and code writes the summary from the replayed results. No Gemini call.

ADK APIs used (google-adk 2.15.1, github.com/google/adk-python): LlmAgent(name, model,
static_instruction, tools, generate_content_config, before/after_tool_callback), Runner
(agent, app_name, session_service, auto_create_session), InMemorySessionService,
RunConfig(max_llm_calls), Event.get_function_calls(), McpToolset +
StreamableHTTPConnectionParams(url, headers)."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, func, select

from server.agent import checker
from server.agent.gemini import CallRecord, GeminiClient, Ledger
from server.agent.tools import SiftTools
from server.connectors.grail import GrailClient
from server.models import Proposal, Run, RunEvent

PHASES: list[tuple[str, str]] = [
    ("ingest", "Reading 7 days of ingest volume"),
    ("dynatrace", "Querying Dynatrace (MCP)"),
    ("usage", "Reading 30 days of query history"),
    ("patterns", "Looking at log patterns"),
    ("rules", "Drafting and validating rules"),
    ("proposals", "Creating proposals"),
]
_INDEX = {k: i + 1 for i, (k, _) in enumerate(PHASES)}
TOOL_PHASE = {
    "get_signals": "ingest",
    "get_usage": "usage",
    "get_patterns": "patterns",
    "record_finding": "patterns",
    "draft_rule": "rules",
    "validate_rule": "rules",
    "estimate_savings": "rules",
    "create_proposal": "proposals",
    "get_proposals": "proposals",
    "apply_proposal": "proposals",
    "summarize_run": "proposals",
}
MAX_GAP_MS = 2500  # replay pacing: never wait longer than this between two events


# --- run log ----------------------------------------------------------------------------


class RunLog:
    """RunEvents of an agent run. Steps are phases in a fixed order; a tool call goes to its
    phase's step, opening it the first time (earlier open steps are closed then)."""

    def __init__(self, session: Session, run_id: str, clock: Callable[[], datetime]) -> None:
        self.session, self.run_id, self.clock = session, run_id, clock
        last = session.exec(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)).one()
        self.seq = last or 0
        self.opened: dict[int, int] = {}  # step -> total tool ms
        self.opened_at: dict[int, datetime] = {}
        self.closed: set[int] = set()
        self.current = 0

    def emit(self, type: str, label: str, **kw: Any) -> None:
        self.seq += 1
        self.session.add(
            RunEvent(
                run_id=self.run_id, seq=self.seq, at=self.clock(), type=type, label=label, **kw
            )
        )
        self.session.commit()

    def _close(self, n: int) -> None:
        if n in self.opened and n not in self.closed:
            self.closed.add(n)
            # the step's wall time (the model thinking included), at least its tool time
            wall = round((self.clock() - self.opened_at[n]).total_seconds() * 1000)
            ms = max(wall, self.opened[n])
            self.emit("step_done", PHASES[n - 1][1], duration_ms=ms, result={"step": n})

    def step(self, phase: str) -> int:
        n = _INDEX[phase]
        if n not in self.opened:
            for m in sorted(self.opened):
                if m < n:
                    self._close(m)
            self.opened[n] = 0
            self.opened_at[n] = self.clock()
            self.emit("step", PHASES[n - 1][1], status="running", result={"step": n})
        self.current = n
        return n

    def tool(self, rec: dict[str, Any], duration_ms: int | None = None) -> None:
        phase = TOOL_PHASE.get(rec["name"], "dynatrace")
        n = self.step(phase)
        ms = rec["duration_ms"] if duration_ms is None else duration_ms
        self.opened[n] += ms
        self.emit(
            "tool",
            rec["label"],
            tool=rec["name"],
            duration_ms=ms,
            rows=rec.get("rows"),
            result={"step": n, "args": rec["args"], "result": rec["result"]},
        )

    def proposal(self, p: Proposal) -> None:
        n = self.step("proposals")
        self.emit("proposal", p.title, result={"step": n, "proposal": p.id})

    def message(self, text: str) -> None:
        self.emit("message", text, result={"step": self.current})

    def llm(self, rec: CallRecord) -> None:
        label = f"Gemini {rec.served_model}: {rec.tokens_in:,} in · {rec.tokens_out:,} out"
        self.emit("llm", label, duration_ms=rec.duration_ms, result=rec.to_json())

    def finish(self) -> None:
        for n in sorted(self.opened):
            self._close(n)


# --- summaries --------------------------------------------------------------------------


def code_summary(tools: SiftTools) -> str:
    """The run summary written by code. It quotes only the `summarize_run` tool result,
    which code calls at the end of every agent run (so the number checker holds here too)."""
    last = tools.calls[-1] if tools.calls else None
    r = last["result"] if last and last["name"] == "summarize_run" else tools.summarize_run()
    n = r["created"]
    if n:
        noun = "proposal" if n == 1 else "proposals"
        parts = [f"Created {n} new {noun} worth {r['created_saves_eur_month']} a month."]
    else:
        parts = ["No new proposals."]
    if r["stopped_by_guardrails"]:
        parts.append(f"Guardrails stopped: {'; '.join(r['stopped_by_guardrails'])}.")
    if r["left_alone_in_use"]:
        parts.append(f"Left alone because people use it: {'; '.join(r['left_alone_in_use'])}.")
    if r["already_proposed_or_decided"]:
        parts.append(f"{r['already_proposed_or_decided']} drafts were already proposed or decided.")
    big = r.get("protected_left_alone", [])[:3]
    if big:
        names = "; ".join(f"{x['name']} ({x['guardrails'][0]})" for x in big)
        parts.append(f"Protected and left alone: {names}.")
    if r["awaiting_approval"]:
        parts.append(
            f"{r['awaiting_approval']} proposals await approval, worth "
            f"{r['awaiting_saves_eur_month']} a month at your rates."
        )
    return " ".join(parts)


def close(tools: SiftTools) -> dict[str, Any]:
    """Code's last tool call of every agent run."""
    return tools.call("summarize_run", {})


# --- live (ADK + Gemini) ----------------------------------------------------------------


@dataclass
class Outcome:
    summary: str
    final_text: str | None
    unbacked: list[str]
    ledger: Ledger
    tools: SiftTools
    calls: list[dict[str, Any]] = field(default_factory=list)  # flat, in order, with t_ms
    texts: list[dict[str, Any]] = field(default_factory=list)  # {"after_calls", "text"}
    diverged: bool = False
    mcp_tools: list[str] = field(default_factory=list)


def adk_functions(t: SiftTools, timeline: list[dict[str, Any]], t0: float) -> list[Callable]:
    """Sift's tools as plain async functions; ADK builds the declarations from the
    signatures and docstrings (FunctionTool)."""

    def run(name: str, args: dict[str, Any]) -> dict[str, Any]:
        result = t.call(name, args)
        timeline.append({**t.calls[-1], "t_ms": round((time.monotonic() - t0) * 1000)})
        return result

    async def get_signals(kind: str = "", service: str = "", limit: int = 40) -> dict:
        """Signals (log sources, log patterns, spans, span attributes, metrics, metric
        dimensions) with today's volume, monthly cost at the user's rates, usage class
        (used | rarely | unused), guardrails and existing proposals; most expensive first.
        kind and service filter the list."""
        return run("get_signals", {"kind": kind, "service": service, "limit": limit})

    async def get_usage(signal_id: str) -> dict:
        """Who reads a signal: dashboards, notebooks, workflows, alerts, SLOs and ad-hoc
        queries (30 days), readers of its parts, neighbours a rule would not affect, and the
        guardrails that protect it."""
        return run("get_usage", {"signal_id": signal_id})

    async def get_patterns(signal_id: str) -> dict:
        """Log templates of a log source (or the pattern itself) with share of volume,
        cost, usage and redacted sample lines."""
        return run("get_patterns", {"signal_id": signal_id})

    async def record_finding(signal_id: str, finding_class: str, note: str = "") -> dict:
        """Record the agent's assessment of a signal (also for data no rule may touch, e.g.
        advice to fix a bug). note: one sentence; numbers only as placeholders {gib_day},
        {records_day}, {gib_month}, {eur_month}, {last_used}, {queries_30d}."""
        return run(
            "record_finding",
            {"signal_id": signal_id, "finding_class": finding_class, "note": note},
        )

    async def draft_rule(
        signal_id: str,
        action: str,
        title: str = "",
        keep_phrase: str = "",
        only_status_code: int = 0,
        metric_name: str = "",
    ) -> dict:
        """Draft a rule for both targets (OpenPipeline processor and OTel Collector config).
        action: drop | drop_field | logs_to_metric | redact. title: short imperative, e.g.
        "Drop DEBUG logs from payment". keep_phrase (logs, drop): lines containing it are
        kept. only_status_code (spans, drop): drop only spans with this HTTP status.
        metric_name (logs_to_metric): e.g. inventory.heartbeat.count."""
        return run(
            "draft_rule",
            {
                "signal_id": signal_id,
                "action": action,
                "title": title,
                "keep_phrase": keep_phrase,
                "only_status_code": only_status_code,
                "metric_name": metric_name,
            },
        )

    async def validate_rule(draft_id: str) -> dict:
        """Check a draft: the OTel config's structure (and otelcol validate when available),
        and the OpenPipeline matcher as a DQL dry run over the last 24 h (records hit,
        records outside the signal, ERROR/FATAL hit)."""
        return run("validate_rule", {"draft_id": draft_id})

    async def estimate_savings(draft_id: str) -> dict:
        """Savings of a validated draft in GiB and € per month at the user's rates, the
        threshold, and the placeholders to use in the explanation."""
        return run("estimate_savings", {"draft_id": draft_id})

    async def create_proposal(
        draft_id: str, finding_class: str, confidence: str, explanation: list[str]
    ) -> dict:
        """Create a proposal awaiting human approval. Guardrails and usage checks run here
        and may reject it with a reason. confidence: high | medium | low. explanation: 3 to
        5 sentences with placeholders for every number."""
        return run(
            "create_proposal",
            {
                "draft_id": draft_id,
                "finding_class": finding_class,
                "confidence": confidence,
                "explanation": list(explanation),
            },
        )

    async def get_proposals(status: str = "") -> dict:
        """Existing proposals (optionally by status: awaiting_approval, verified, rejected,
        reverted, ...) with savings and decisions."""
        return run("get_proposals", {"status": status})

    async def apply_proposal(proposal_id: str) -> dict:
        """Applying needs a person: this only reports that the proposal awaits approval."""
        return run("apply_proposal", {"proposal_id": proposal_id})

    return [
        get_signals,
        get_usage,
        get_patterns,
        record_finding,
        draft_rule,
        validate_rule,
        estimate_savings,
        create_proposal,
        get_proposals,
        apply_proposal,
    ]


def _mcp_toolset(url: str, token: str) -> Any:
    """Remote Dynatrace MCP server (Bearer platform token). The tool list comes from the
    server's tools/list; Sift does not name or filter tools.
    https://docs.dynatrace.com/docs/shortlink/dynatrace-mcp-server"""
    from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams

    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
    )


async def run_live(
    tools: SiftTools,
    gemini: GeminiClient,
    *,
    model: str,
    instruction: str,
    message: str,
    max_tokens: int,
    max_llm_calls: int,
    on_llm: Callable[[CallRecord], None] | None = None,
    on_text: Callable[[str], None] | None = None,
    mcp: tuple[str, str] | None = None,
    on_mcp: Callable[[dict[str, Any]], None] | None = None,
) -> Outcome:
    from google.adk.agents import LlmAgent, RunConfig
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from server.agent.llm import SiftGemini

    ledger = Ledger(max_tokens=max_tokens, on_call=on_llm)
    t0 = time.monotonic()
    timeline: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []
    mcp_names: list[str] = []
    started: dict[str, float] = {}
    toolset = _mcp_toolset(*mcp) if mcp else None

    def before_tool(tool: Any, args: dict[str, Any], ctx: Any) -> None:
        started[ctx.function_call_id or tool.name] = time.monotonic()

    def after_tool(tool: Any, args: dict[str, Any], ctx: Any, resp: Any) -> None:
        if tool.name in SiftTools.NAMES:
            return
        t = started.pop(ctx.function_call_id or tool.name, time.monotonic())
        text = json.dumps(resp, default=str, ensure_ascii=False)
        rec = {
            "name": f"mcp:{tool.name}",
            "args": dict(args),
            "result": {"content": text[:20_000]},
            "duration_ms": round((time.monotonic() - t) * 1000),
            "label": f"Dynatrace MCP: {tool.name}",
            "rows": None,
            "t_ms": round((t - t0) * 1000),
        }
        mcp_names.append(tool.name)
        tools.calls.append(rec)
        timeline.append(rec)
        if on_mcp:
            on_mcp(rec)

    agent = LlmAgent(
        name="sift",
        model=SiftGemini(model=model, gemini=gemini, ledger=ledger),
        static_instruction=instruction,
        tools=[*adk_functions(tools, timeline, t0), *([toolset] if toolset else [])],
        generate_content_config=types.GenerateContentConfig(
            max_output_tokens=8192,
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
        ),
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
    )
    runner = Runner(
        agent=agent,
        app_name="sift",
        session_service=InMemorySessionService(),
        auto_create_session=True,
    )
    final: str | None = None
    try:
        async for ev in runner.run_async(
            user_id="sift",
            session_id=f"s{int(t0 * 1000)}",
            new_message=types.Content(role="user", parts=[types.Part(text=message)]),
            run_config=RunConfig(max_llm_calls=max_llm_calls),
        ):
            if ev.partial or not ev.content or ev.content.role != "model":
                continue
            text = "".join(p.text for p in ev.content.parts or [] if p.text and not p.thought)
            if text.strip():
                texts.append({"after_calls": len(timeline), "text": text.strip()})
                final = text.strip()
                if on_text:
                    on_text(text.strip())
    finally:
        if toolset is not None:
            await toolset.close()
    # Only the last text counts as the answer if no tool ran after it.
    if texts and texts[-1]["after_calls"] != len(timeline):
        final = None
    close(tools)
    missing = checker.unbacked([final], tools.sources()) if final else []
    summary = final if final and not missing else code_summary(tools)
    return Outcome(
        summary=summary,
        final_text=final,
        unbacked=missing,
        ledger=ledger,
        tools=tools,
        calls=timeline,
        texts=texts,
        mcp_tools=sorted(set(mcp_names)),
    )


# --- recordings -------------------------------------------------------------------------


def recording(outcome: Outcome, kind: str, state: str, at: datetime, **extra: Any) -> dict:
    lg = outcome.ledger
    return {
        "_about": "A real Gemini run on Vertex AI, recorded by `python -m server.cli agent`. "
        "Fixture mode replays the tool calls through the same tools (no Gemini call) and "
        "shows the texts only if every replayed result matches this recording.",
        "kind": kind,
        "state": state,
        "recorded_at": at.isoformat(),
        **extra,
        "usage": {
            "requested_model": lg.requested_model,
            "served_model": lg.served_model,
            "fallback_reason": lg.fallback_reason,
            "llm_calls": len(lg.calls),
            "tokens_in": lg.tokens_in,
            "tokens_out": lg.tokens_out,
            "cached_tokens": sum(c.cached for c in lg.calls),
            "cost_usd": lg.cost_usd,
        },
        "dynatrace_mcp_tools_called": outcome.mcp_tools,
        "final_text": outcome.final_text,
        "final_text_unbacked_numbers": outcome.unbacked,
        "texts": outcome.texts,
        "calls": outcome.calls,
        "llm": [c.to_json() for c in lg.calls],
    }


def load_recording(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text()) if path.exists() else None


def _same(a: Any, b: Any) -> bool:
    return checker.dumps(a) == checker.dumps(b)


def replay(
    session: Session,
    grail: GrailClient,
    clock: Callable[[], datetime],
    rec: dict[str, Any],
    *,
    run_id: str | None,
    log: RunLog | None,
    user_message: str = "",
    otelcol_bin: str | None = None,
) -> Iterator[int]:
    """Re-run a recording's tool calls. Yields ms to wait between events (recorded pacing,
    capped); the result is in `replay.outcome` of the generator's return (StopIteration)."""
    current: list[int] = [0]
    tools = SiftTools(
        session,
        grail,
        clock,
        run_id=run_id,
        on_tool=(lambda r: log.tool(r, duration_ms=current[0])) if log else None,
        on_proposal=log.proposal if log else None,
        otelcol_bin=otelcol_bin,
        user_message=user_message,
    )
    texts: dict[int, list[str]] = {}
    for t in rec.get("texts", []):
        texts.setdefault(int(t["after_calls"]), []).append(t["text"])
    diverged = False
    last_t = 0
    calls = rec.get("calls", [])
    for i, call in enumerate(calls):
        for text in texts.get(i, []):
            if log and not diverged:
                log.message(text)
        yield min(max(int(call.get("t_ms", 0)) - last_t, 0), MAX_GAP_MS)
        last_t = int(call.get("t_ms", 0))
        current[0] = int(call.get("duration_ms", 0))
        if call["name"].startswith("mcp:"):
            tools.calls.append(dict(call))  # recorded Dynatrace MCP answer, not re-queried
            if log:
                log.tool(call)
            continue
        result = tools.call(call["name"], call["args"])
        if not _same(result, call["result"]):
            diverged = True
    close(tools)
    final = rec.get("final_text")
    missing = checker.unbacked([final], tools.sources()) if final else []
    if final and not diverged and not missing:
        for text in texts.get(len(calls), []):
            if log and text != final:
                log.message(text)
        if log:
            log.message(final)
        summary = final
    else:
        summary = code_summary(tools)
    return Outcome(
        summary=summary,
        final_text=final if not diverged else None,
        unbacked=missing,
        ledger=Ledger(),
        tools=tools,
        calls=calls,
        diverged=diverged,
        mcp_tools=list(rec.get("dynatrace_mcp_tools_called", [])),
    )


def run_replay_sync(gen: Iterator[int], on_wait: Callable[[int], None]) -> Outcome:
    """Drive a replay generator; `on_wait` advances a clock or sleeps."""
    try:
        while True:
            on_wait(next(gen))
    except StopIteration as stop:
        return stop.value


async def run_replay_async(gen: Iterator[int], wait: Callable[[int], Awaitable[None]]) -> Outcome:
    try:
        while True:
            await wait(next(gen))
    except StopIteration as stop:
        return stop.value


def apply_outcome(run: Run, out: Outcome, rec: dict[str, Any] | None = None) -> None:
    """Fill a run's summary and Gemini usage (from the live ledger or the recording)."""
    run.summary = out.summary
    if rec is not None:
        u = rec.get("usage", {})
        run.requested_model = u.get("requested_model")
        run.served_model = u.get("served_model")
        run.fallback_reason = u.get("fallback_reason")
        run.tokens_in, run.tokens_out = int(u.get("tokens_in", 0)), int(u.get("tokens_out", 0))
        run.cost_usd, run.llm_calls = u.get("cost_usd"), u.get("llm_calls")
        return
    lg = out.ledger
    run.requested_model, run.served_model = lg.requested_model, lg.served_model
    run.fallback_reason = lg.fallback_reason
    run.tokens_in, run.tokens_out = lg.tokens_in, lg.tokens_out
    run.cost_usd, run.llm_calls = lg.cost_usd, len(lg.calls)
    run.scripted = False
