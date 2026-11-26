"""Agent runs. Fixture mode replays a scripted run: the steps and pacing are scripted, every
number is computed by the engine while the run replays, and no Gemini call is made.

A script is a generator that writes RunEvents and yields how many milliseconds to wait
before the next event. The same script drives the live replay (real sleeps) and seeding
(a virtual clock)."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Session, func, select

from server import engine, fixtures, fmt, services
from server.config import Config
from server.db import utcnow
from server.models import Proposal, Run, RunEvent

RUN_TYPES = ("discover", "analyze", "verify")
# Steps each script emits; the UI shows progress as done / total.
STEP_COUNTS = {"analyze": 7, "discover": 2, "verify": 1}


@dataclass
class Tool:
    tool: str
    label: str
    duration_ms: int
    rows: int | None = None
    result: dict[str, Any] = field(default_factory=dict)


class Replay:
    def __init__(self, session: Session, cfg: Config, run: Run, clock: Callable[[], datetime]):
        self.session, self.cfg, self.run, self.clock = session, cfg, run, clock
        self.seq = 0
        self.step_no = 0
        self.results: dict[str, dict[str, Any]] = {}

    def world(self) -> engine.World:
        return engine.load_world(self.session, self.clock())

    def emit(self, type: str, label: str, **kw: Any) -> None:
        self.seq += 1
        self.session.add(
            RunEvent(
                run_id=self.run.id, seq=self.seq, at=self.clock(), type=type, label=label, **kw
            )
        )
        self.session.commit()

    def step(self, label: str, tools: list[Callable[[], Tool]]) -> Iterator[int]:
        self.step_no += 1
        n = self.step_no
        self.emit("step", label, result={"step": n}, status="running")
        total = 0
        for make in tools:
            t = make()
            yield t.duration_ms
            total += t.duration_ms
            self.results[t.tool] = t.result
            self.emit(
                "tool",
                t.label,
                tool=t.tool,
                duration_ms=t.duration_ms,
                rows=t.rows,
                result={"step": n, **t.result},
            )
        self.emit("step_done", label, duration_ms=total, result={"step": n})


# --- scripts ----------------------------------------------------------------------------


def _ingest_tool(r: Replay) -> Tool:
    w = r.world()
    week = {dt: 0.0 for dt in ("logs", "spans", "metrics")}
    for sig in engine.partition(w):
        week[sig.data_type] += sum(v for _, v in engine.daily_series(w, sig, 7))
    total = sum(week.values()) + sum(w.unattributed().values()) * 7
    return Tool(
        "get_signals",
        "Queried Dynatrace (DQL) for ingest volume",
        1300,
        rows=len(w.signals),
        result={
            "signals": len(w.signals),
            "ingest_7d": fmt.gib(total),
            **{k: fmt.gib(v) for k, v in week.items()},
        },
    )


def _usage_tool(r: Replay) -> Tool:
    w = r.world()
    refs = [ref for lst in w.refs.values() for ref in lst]
    unused = [s for s in w.signals.values() if engine.usage(w, s).klass == "unused"]
    queries = sum(ref.count_30d for ref in refs if ref.type == "query")
    return Tool(
        "get_usage",
        "Read query executions, dashboards, notebooks, workflows and alerts",
        1700,
        rows=len(refs),
        result={
            "references": len(refs),
            "ad_hoc_queries_30d": queries,
            "unused_signals": len(unused),
        },
    )


def _parse_tool(r: Replay, model: str | None) -> Tool:
    return Tool(
        "gemini_parse_query",
        "Asked Gemini to read 1 dashboard query the parser could not resolve",
        900,
        rows=1,
        result={"queries": 1, "model": model or "fixture"},
    )


def _patterns_tool(r: Replay) -> Tool:
    w = r.world()
    patterns = [s for s in w.signals.values() if s.kind == "log_pattern"]
    return Tool(
        "get_patterns",
        "Clustered log lines into templates (Drain3)",
        1100,
        rows=len(patterns),
        result={
            "patterns": len(patterns),
            "labels": {s.key: s.finding_class or "none" for s in patterns},
        },
    )


def _label_tool(r: Replay, model: str | None) -> Tool:
    w = r.world()
    patterns = [s for s in w.signals.values() if s.kind == "log_pattern"]
    return Tool(
        "gemini_label_patterns",
        f"Asked Gemini to label {len(patterns)} log patterns",
        1400,
        rows=len(patterns),
        result={"labeled": len(patterns), "model": model or "fixture"},
    )


def _guardrail_tool(r: Replay) -> Tool:
    w = r.world()
    protected = [s for s in w.signals.values() if engine.guardrail_hits(w, s)]
    return Tool(
        "guardrails",
        f"Checked {len(w.guardrails)} guardrails",
        300,
        rows=len(protected),
        result={
            "guardrails": len(w.guardrails),
            "protected": len(protected),
            "protected_signals": [f"{s.service} {s.key}" for s in protected],
        },
    )


def _estimate_tool(r: Replay, candidates: list[str]) -> Tool:
    w = r.world()
    defs = fixtures.proposal_definitions(r.cfg)
    threshold = float(w.settings.get("min_savings_eur_month", 0))
    covered = {p.signal_id for p in w.proposals} | {defs[c]["signal_id"] for c in candidates}
    skipped = [
        s
        for s in w.signals.values()
        if s.id not in covered
        and engine.usage(w, s).klass == "unused"
        and not engine.guardrail_hits(w, s)
        and engine.cost_month(w, s) < threshold
    ]
    total = 0.0
    for c in candidates:
        p = w.proposal(c) or Proposal(
            **{k: v for k, v in defs[c].items() if k in Proposal.model_fields},
            status="draft",
            created_at=r.clock(),
        )
        total += engine.savings(w, p).eur_month
    return Tool(
        "estimate_savings",
        f"Estimated savings for {len(candidates)} findings",
        500,
        rows=len(candidates),
        result={
            "findings": len(candidates),
            "eur_month": fmt.eur(total),
            "skipped_below_threshold": len(skipped),
            "threshold": fmt.eur(threshold),
        },
    )


def _validate_tool(r: Replay, pid: str) -> Tool:
    w = r.world()
    defs = fixtures.proposal_definitions(r.cfg)
    p = w.proposal(pid) or Proposal(
        **{k: v for k, v in defs[pid].items() if k in Proposal.model_fields},
        status="draft",
        created_at=r.clock(),
    )
    d = engine.dry_run(w, p)
    return Tool(
        "validate_rule",
        f"Dry run for “{p.title}”: hits {fmt.count(d.matched_records_day)} of "
        f"{fmt.count(d.scope_records_day)} {d.scope_label} a day",
        700,
        rows=round(d.matched_records_day),
        result={
            "proposal": pid,
            "matched": fmt.count(d.matched_records_day),
            "scope": fmt.count(d.scope_records_day),
            "errors_matched": round(d.errors_matched),
        },
    )


def _create_tool(r: Replay, pid: str) -> Tool:
    defs = fixtures.proposal_definitions(r.cfg)
    p = services.create_proposal(r.session, defs[pid], r.clock(), run_id=r.run.id)
    r.emit("proposal", p.title, result={"proposal": p.id})
    return Tool(
        "create_proposal", f"Created proposal “{p.title}”", 250, rows=1, result={"proposal": p.id}
    )


def _open_tool(r: Replay, created: int) -> Tool:
    w = r.world()
    awaiting = [p for p in w.proposals if p.status == "awaiting_approval"]
    eur = sum(engine.savings(w, p).eur_month for p in awaiting)
    return Tool(
        "get_proposals",
        f"{len(awaiting)} proposals await approval",
        200,
        rows=len(awaiting),
        result={"created": created, "awaiting": len(awaiting), "awaiting_eur_month": fmt.eur(eur)},
    )


def _analyze(r: Replay, spec: dict[str, Any]) -> Iterator[int]:
    candidates: list[str] = spec["candidates"]
    model = spec.get("served_model")
    yield from r.step("Reading 7 days of ingest volume", [lambda: _ingest_tool(r)])
    yield from r.step(
        "Reading 30 days of query history", [lambda: _usage_tool(r), lambda: _parse_tool(r, model)]
    )
    yield from r.step(
        "Clustering log lines into patterns",
        [lambda: _patterns_tool(r), lambda: _label_tool(r, model)],
    )
    yield from r.step("Checking guardrails", [lambda: _guardrail_tool(r)])
    yield from r.step("Estimating savings", [lambda: _estimate_tool(r, candidates)])
    yield from r.step(
        "Drafting and validating rules", [(lambda c=c: _validate_tool(r, c)) for c in candidates]
    )
    existing = {p.id for p in r.world().proposals}
    new = [c for c in candidates if c not in existing]
    makers: list[Callable[[], Tool]] = [(lambda c=c: _create_tool(r, c)) for c in new]
    makers.append(lambda: _open_tool(r, len(new)))
    yield from r.step("Creating proposals", makers)

    g, o = r.results["guardrails"], r.results["get_proposals"]
    e = r.results["estimate_savings"]
    created = o["created"]
    first = (
        (f"{created} new proposals." if created != 1 else "1 new proposal.")
        if created
        else ("No new findings: every finding already has a proposal.")
    )
    r.run.summary = (
        f"{first} {o['awaiting']} proposals await approval, worth "
        f"{o['awaiting_eur_month']} a month at your rates. {g['protected']} signals are "
        f"protected by guardrails, and {e['skipped_below_threshold']} unused signals are "
        f"below the {e['threshold']} a month threshold."
    )


def _discover(r: Replay, spec: dict[str, Any]) -> Iterator[int]:
    yield from r.step("Reading 7 days of ingest volume", [lambda: _ingest_tool(r)])

    def cost() -> Tool:
        w = r.world()
        total = sum(engine.cost_month(w, s) for s in engine.partition(w))
        total += sum(w.cost_model.monthly(k, v) for k, v in w.unattributed().items())
        return Tool(
            "cost_model",
            "Applied your rates",
            300,
            rows=len(w.signals),
            result={"eur_month": fmt.eur(total)},
        )

    yield from r.step("Estimating cost at your rates", [cost])
    i, c = r.results["get_signals"], r.results["cost_model"]
    r.run.summary = (
        f"Ingest in the last 7 days: {i['ingest_7d']} across {i['signals']} signals, "
        f"about {c['eur_month']} a month at your rates."
    )


def _verify(r: Replay, spec: dict[str, Any]) -> Iterator[int]:
    pending = [p.id for p in r.world().proposals if p.status == "verifying"]
    outcomes: list[str] = []

    def check(pid: str) -> Tool:
        p = services.verify(r.session, pid, r.clock())
        outcomes.append(p.status)
        verb = "Verified" if p.status == "verified" else "Reverted"
        return Tool(
            "verify_proposal",
            f"{verb} “{p.title}”",
            900,
            rows=3,
            result={
                "proposal": pid,
                "status": p.status,
                "verified_so_far": outcomes.count("verified"),
                "checked_so_far": len(outcomes),
            },
        )

    if not pending:
        yield from r.step("Looking for rules to verify", [])
        r.run.summary = "Nothing is waiting for verification."
        return
    yield from r.step(
        "Checking volume, references and Davis problems",
        [(lambda pid=pid: check(pid)) for pid in pending],
    )
    r.run.summary = f"{outcomes.count('verified')} of {len(outcomes)} rules verified."


SCRIPTS = {"analyze": _analyze, "discover": _discover, "verify": _verify}


# --- drivers ----------------------------------------------------------------------------


def new_run(session: Session, cfg: Config, type: str, trigger: str, at: datetime) -> Run:
    if type not in RUN_TYPES:
        raise services.ConflictError(f"unknown run type {type!r}")
    spec = fixtures.load(cfg, "runs")[type]
    run = Run(
        id=f"run_{secrets.token_hex(4)}",
        type=type,
        status="running",
        trigger=trigger,
        started_at=at,
        requested_model=spec["requested_model"],
        served_model=spec["served_model"],
        fallback_reason=spec["fallback_reason"],
        scripted=True,
    )
    session.add(run)
    services.audit(
        session,
        at,
        "agent" if trigger == "schedule" else _user(session),
        "run_started",
        "run",
        run.id,
        f"Started {_label(type)}",
    )
    session.commit()
    return run


def _finish(session: Session, run: Run, spec: dict[str, Any], at: datetime) -> None:
    run.status, run.finished_at = "succeeded", at
    run.tokens_in, run.tokens_out = spec["tokens_in"], spec["tokens_out"]
    session.add(run)
    services.audit(
        session,
        at,
        "agent",
        "run_finished",
        "run",
        run.id,
        f"Finished {_label(run.type)}. {run.summary}",
    )
    session.commit()


def _fail(session: Session, run: Run, at: datetime, reason: str) -> None:
    run.status, run.finished_at, run.summary = "failed", at, reason
    session.add(run)
    services.audit(
        session,
        at,
        "agent",
        "run_failed",
        "run",
        run.id,
        f"{_label(run.type).capitalize()} failed: {reason}",
    )
    session.commit()


def _label(type: str) -> str:
    return {"analyze": "analysis", "discover": "discovery", "verify": "verification"}[type]


def _user(session: Session) -> str:
    return str(services.get_settings(session).get("user_name", "user"))


def replay_sync(session: Session, cfg: Config, type: str, trigger: str, at: datetime) -> Run:
    """Seeding: run the script on a virtual clock that starts at `at`."""
    run = new_run(session, cfg, type, trigger, at)
    clock = [at]
    r = Replay(session, cfg, run, lambda: clock[0])
    spec = fixtures.load(cfg, "runs")[type]
    for delay in SCRIPTS[type](r, spec):
        clock[0] += timedelta(milliseconds=delay)
    _finish(session, run, spec, clock[0])
    r.emit("end", run.status)
    return run


async def replay_async(db: Engine, cfg: Config, run_id: str) -> None:
    with Session(db, expire_on_commit=False) as session:
        run = session.get(Run, run_id)
        assert run is not None
        spec = fixtures.load(cfg, "runs")[run.type]
        r = Replay(session, cfg, run, utcnow)
        try:
            for delay in SCRIPTS[run.type](r, spec):
                if cfg.replay_speed > 0:
                    await asyncio.sleep(delay / 1000 / cfg.replay_speed)
            _finish(session, run, spec, utcnow())
        except asyncio.CancelledError:
            _fail(session, run, utcnow(), "Interrupted: the server stopped")
            raise
        except Exception as exc:  # noqa: BLE001 (a failed run must never leave the UI spinning)
            session.rollback()
            _fail(session, run, utcnow(), f"{type(exc).__name__}: {exc}")
        finally:
            r.emit("end", run.status)


def running_run(session: Session) -> Run | None:
    return session.exec(select(Run).where(Run.status == "running")).first()


def fail_interrupted(session: Session, now: datetime) -> None:
    """After a restart, runs that were replaying cannot resume."""
    for run in session.exec(select(Run).where(Run.status == "running")).all():
        last = session.exec(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run.id)).one()
        _fail(session, run, now, "Interrupted: the server restarted")
        session.add(
            RunEvent(run_id=run.id, seq=(last or 0) + 1, at=now, type="end", label="failed")
        )
        session.commit()
