"""Agent runs.

analyze: the Sift agent (server.agent). With SIFT_AGENT=gemini it runs live on Vertex AI;
otherwise (fixture mode, and always while seeding) it replays the recorded real run
fixtures/agent/analyze.json through the same tools, with no Gemini call.
discover and verify: scripted, every number computed by the engine while they run.

A script is a generator that writes RunEvents and yields how many milliseconds to wait
before the next event. The same script drives the live replay (real sleeps) and seeding
(a virtual clock)."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Session, func, select

from server import engine, fixtures, fmt, services
from server.agent import prompts
from server.agent import runner as agent_runner
from server.agent.gemini import GeminiClient
from server.agent.tools import SiftTools
from server.config import Config
from server.connectors import fixture_connectors
from server.connectors.grail import GrailClient
from server.db import utcnow
from server.models import Run, RunEvent

RUN_TYPES = ("discover", "analyze", "verify")
# Steps each scripted run emits; the UI shows progress as done / total. Agent runs report
# the steps they have opened so far (server.agent.runner.PHASES).
STEP_COUNTS = {"discover": 2, "verify": 1}
ANALYZE_RECORDING = "agent/analyze.json"


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


SCRIPTS = {"discover": _discover, "verify": _verify}


# --- drivers ----------------------------------------------------------------------------


def analyze_recording(cfg: Config) -> dict[str, Any]:
    rec = agent_runner.load_recording(cfg.fixtures_dir / ANALYZE_RECORDING)
    if rec is None:
        raise services.ConflictError(
            f"no recorded analysis at fixtures/{ANALYZE_RECORDING}; record one with "
            "`make agent-record` (needs Vertex AI)"
        )
    return rec


def new_run(
    session: Session, cfg: Config, type: str, trigger: str, at: datetime, *, live: bool = True
) -> Run:
    """live=False (seeding) always replays: seeding must stay offline and free."""
    if type not in RUN_TYPES:
        raise services.ConflictError(f"unknown run type {type!r}")
    run = Run(
        id=f"run_{secrets.token_hex(4)}",
        type=type,
        status="running",
        trigger=trigger,
        started_at=at,
        scripted=True,
    )
    if type == "analyze" and live and cfg.agent == "gemini":
        run.requested_model, run.scripted = cfg.gemini_model, False
    elif type == "analyze":
        u = analyze_recording(cfg)["usage"]
        run.requested_model, run.served_model = u["requested_model"], u["served_model"]
        run.fallback_reason, run.replay_of = u["fallback_reason"], ANALYZE_RECORDING
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


def _finish(session: Session, run: Run, spec: dict[str, Any] | None, at: datetime) -> None:
    run.status, run.finished_at = "succeeded", at
    if spec is not None:
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


def _grail(cfg: Config, now: datetime, record_dry_runs: Path | None = None) -> GrailClient:
    conn = fixture_connectors(cfg.fixtures_dir / "dynatrace", now)
    if record_dry_runs is not None:
        from server.synth import dry_run_recorder

        conn.grail.on_missing = dry_run_recorder(record_dry_runs).query  # type: ignore[attr-defined]
    return conn.grail


def replay_sync(session: Session, cfg: Config, type: str, trigger: str, at: datetime) -> Run:
    """Seeding: run the script on a virtual clock that starts at `at`."""
    run = new_run(session, cfg, type, trigger, at, live=False)
    clock = [at]

    def now() -> datetime:
        return clock[0]

    if type == "analyze":
        rec = analyze_recording(cfg)
        log = agent_runner.RunLog(session, run.id, now)
        gen = agent_runner.replay(session, _grail(cfg, at), now, rec, run_id=run.id, log=log)

        def advance(ms: int) -> None:
            clock[0] += timedelta(milliseconds=ms)

        out = agent_runner.run_replay_sync(gen, advance)
        log.finish()
        agent_runner.apply_outcome(run, out, rec)
        _finish(session, run, None, clock[0])
        log.emit("end", run.status)
        return run
    r = Replay(session, cfg, run, now)
    spec = fixtures.load(cfg, "runs")[type]
    for delay in SCRIPTS[type](r, spec):
        clock[0] += timedelta(milliseconds=delay)
    _finish(session, run, spec, clock[0])
    r.emit("end", run.status)
    return run


async def _analyze(session: Session, cfg: Config, run: Run) -> agent_runner.RunLog:
    log = agent_runner.RunLog(session, run.id, utcnow)
    if run.replay_of:
        rec = analyze_recording(cfg)
        gen = agent_runner.replay(
            session, _grail(cfg, utcnow()), utcnow, rec, run_id=run.id, log=log
        )

        async def wait(ms: int) -> None:
            if cfg.replay_speed > 0:
                await asyncio.sleep(ms / 1000 / cfg.replay_speed)

        out = await agent_runner.run_replay_async(gen, wait)
        log.finish()
        agent_runner.apply_outcome(run, out, rec)
        return log
    gemini = GeminiClient.vertex(
        cfg.gcp_project, cfg.gcp_location, cfg.gemini_model, cfg.gemini_fast_model
    )
    tools = SiftTools(
        session,
        _grail(cfg, utcnow(), record_dry_runs=cfg.db_path.parent / "dry_runs"),
        utcnow,
        run_id=run.id,
        on_tool=log.tool,
        on_proposal=log.proposal,
        otelcol_bin=cfg.otelcol_bin,
    )
    mcp = (cfg.dt_mcp_url, cfg.dt_platform_token) if cfg.dt_mcp_url else None
    out = await agent_runner.run_live(
        tools,
        gemini,
        model=cfg.gemini_model,
        instruction=prompts.ANALYZE,
        message=prompts.ANALYZE_TASK,
        max_tokens=cfg.gemini_max_tokens_per_run,
        max_llm_calls=cfg.gemini_max_llm_calls,
        on_llm=log.llm,
        on_text=log.message,
        mcp=mcp,
        on_mcp=log.tool,
    )
    if out.unbacked:
        log.message(
            "Summary written by code: the agent's closing text had numbers without a "
            "source (" + ", ".join(out.unbacked) + ")."
        )
    log.finish()
    agent_runner.apply_outcome(run, out)
    return log


async def replay_async(db: Engine, cfg: Config, run_id: str) -> None:
    with Session(db, expire_on_commit=False) as session:
        run = session.get(Run, run_id)
        assert run is not None
        log: Any = None
        try:
            if run.type == "analyze":
                log = await _analyze(session, cfg, run)
                _finish(session, run, None, utcnow())
            else:
                spec = fixtures.load(cfg, "runs")[run.type]
                log = Replay(session, cfg, run, utcnow)
                for delay in SCRIPTS[run.type](log, spec):
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
            if log is None:
                log = agent_runner.RunLog(session, run.id, utcnow)
            log.emit("end", run.status)


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
