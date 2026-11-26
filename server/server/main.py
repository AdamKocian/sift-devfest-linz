"""Sift API (FastAPI). In fixture mode everything runs offline from fixtures/.

The web app talks to this API through the Vite dev proxy (`/api`), or is served from
web/dist by this process, so the API needs no CORS."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from server import chat, engine, fixtures, runs, services, views
from server.config import Config, load_config
from server.db import make_engine, utcnow
from server.models import AuditEvent, Proposal, Run, RunEvent, Signal

log = logging.getLogger("sift")
VERSION = "0.1.0"


# --- request bodies ---------------------------------------------------------------------


class DecisionBody(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class RunBody(BaseModel):
    type: str = Field(pattern="^(discover|analyze|verify)$")


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    proposal_id: str | None = None
    signal_id: str | None = None


class Rates(BaseModel):
    logs: float | None = Field(default=None, ge=0, le=100)
    spans: float | None = Field(default=None, ge=0, le=100)
    metrics: float | None = Field(default=None, ge=0, le=100)
    events: float | None = Field(default=None, ge=0, le=100)


class GeminiRates(BaseModel):
    input: float | None = Field(default=None, ge=0, le=1000)
    output: float | None = Field(default=None, ge=0, le=1000)


class SettingsPatch(BaseModel):
    user_name: str | None = Field(default=None, min_length=1, max_length=80)
    user_role: str | None = Field(default=None, max_length=80)
    rates_eur_per_gib: Rates | None = None
    gemini_usd_per_m_tokens: GeminiRates | None = None
    verify_window_min: int | None = Field(default=None, ge=1, le=1440)
    schedule_every_hours: int | None = Field(default=None, ge=0, le=168)
    agent_paused: bool | None = None
    min_savings_eur_month: float | None = Field(default=None, ge=0, le=100000)
    fixture_verify_seconds: float | None = Field(default=None, ge=1, le=600)
    fixture_fail_next_verification: bool | None = None


class GuardrailBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=500)
    service: str | None = Field(default=None, max_length=80)
    kind: str | None = Field(
        default=None,
        pattern="^(log_source|log_pattern|span|span_attribute|metric|metric_dimension)$",
    )
    key_pattern: str | None = Field(default=None, max_length=200)


# --- helpers ----------------------------------------------------------------------------


def _db(request: Request) -> Engine:
    return request.app.state.db


def _cfg(request: Request) -> Config:
    return request.app.state.cfg


def _session(request: Request) -> Session:
    return Session(_db(request), expire_on_commit=False)


def _actor(session: Session) -> str:
    return str(services.get_settings(session).get("user_name") or "user")


def sse(event: str, data: Any, id: int | None = None) -> str:
    head = f"id: {id}\n" if id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _start_run(request: Request, session: Session, type: str, trigger: str) -> Run:
    if runs.running_run(session) is not None:
        raise services.ConflictError("a run is already in progress")
    cfg = _cfg(request)
    run = runs.new_run(session, cfg, type, trigger, utcnow())
    _spawn(request.app, run.id)
    return run


def _spawn(app: FastAPI, run_id: str) -> None:
    task = asyncio.get_running_loop().create_task(
        runs.replay_async(app.state.db, app.state.cfg, run_id)
    )
    app.state.tasks.add(task)
    task.add_done_callback(app.state.tasks.discard)


# --- routes -----------------------------------------------------------------------------

api = APIRouter(prefix="/api")


@api.get("/health")
def health(request: Request) -> dict[str, str]:
    return {"status": "ok", "version": VERSION, "mode": _cfg(request).mode}


@api.get("/meta")
def meta(request: Request) -> dict[str, Any]:
    cfg = _cfg(request)
    with _session(request) as s:
        settings = services.get_settings(s)
        awaiting = len(s.exec(select(Proposal).where(Proposal.status == "awaiting_approval")).all())
        env = fixtures.environment(cfg)
        return {
            "version": VERSION,
            "mode": cfg.mode,
            "apply_target": cfg.apply_target,
            "environment": {"id": cfg.env_id, "name": env["name"]},
            "user": {"name": settings.get("user_name"), "role": settings.get("user_role")},
            "awaiting_count": awaiting,
            "chat_suggestions": chat.SUGGESTIONS,
        }


@api.get("/overview")
def get_overview(request: Request) -> dict[str, Any]:
    with _session(request) as s:
        return views.overview(s, _cfg(request), engine.load_world(s, utcnow()))


@api.get("/signals")
def list_signals(
    request: Request,
    kind: str | None = None,
    service: str | None = None,
    usage: str | None = Query(default=None, pattern="^(unused|rarely|used)$"),
    status: str | None = None,
    q: str | None = None,
    sort: str = Query(default="cost", pattern="^(cost|volume|last_used|name)$"),
) -> dict[str, Any]:
    with _session(request) as s:
        w = engine.load_world(s, utcnow())
        rows = [views.signal_summary(w, sig) for sig in w.signals.values()]
        facets = {
            "kinds": sorted({r["kind"] for r in rows}),
            "services": sorted({r["service"] for r in rows}),
        }
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        if service:
            rows = [r for r in rows if r["service"] == service]
        if usage:
            rows = [r for r in rows if r["usage"]["klass"] == usage]
        if status:
            rows = [r for r in rows if r["status"] == status]
        if q:
            needle = q.lower()
            rows = [r for r in rows if needle in f"{r['service']} {r['key']} {r['kind']}".lower()]
        key = {
            "cost": lambda r: -r["cost_month"],
            "volume": lambda r: -r["gib_day"],
            "last_used": lambda r: r["usage"]["last_used"] or "",
            "name": lambda r: (r["service"], r["key"]),
        }[sort]
        rows.sort(key=key)  # type: ignore[arg-type]
        return {"items": rows, "total": len(w.signals), "facets": facets}


@api.get("/signals/{sid}")
def get_signal(request: Request, sid: str) -> dict[str, Any]:
    with _session(request) as s:
        w = engine.load_world(s, utcnow())
        sig = w.signals.get(sid)
        if sig is None:
            raise services.NotFoundError(f"signal {sid} not found")
        return views.signal_detail(w, sig)


@api.get("/proposals")
def list_proposals(request: Request, status: str | None = None) -> dict[str, Any]:
    with _session(request) as s:
        w = engine.load_world(s, utcnow())
        items = [views.proposal_summary(w, p) for p in w.proposals]
        counts = {
            g: sum(1 for p in items if p["status"] in sts) for g, sts in views.STATUS_GROUPS.items()
        }
        if status:
            wanted = views.STATUS_GROUPS.get(status, (status,))
            items = [p for p in items if p["status"] in wanted]
        items.sort(key=lambda p: (-p["savings"]["eur_month"], p["title"]))
        if status in ("verified", "reverted", "rejected", "applied"):
            items.sort(key=lambda p: p["decided_at"] or p["created_at"], reverse=True)
        return {"items": items, "counts": counts}


@api.get("/proposals/{pid}")
def get_proposal(request: Request, pid: str) -> dict[str, Any]:
    with _session(request) as s:
        w = engine.load_world(s, utcnow())
        p = w.proposal(pid)
        if p is None:
            raise services.NotFoundError(f"proposal {pid} not found")
        return views.proposal_detail(s, _cfg(request), w, p)


def _decide(request: Request, pid: str, action: str, body: DecisionBody) -> dict[str, Any]:
    cfg = _cfg(request)
    with _session(request) as s:
        actor, now = _actor(s), utcnow()
        if action == "approve":
            services.approve(s, cfg, pid, actor, now)
        elif action == "reject":
            services.reject(s, pid, actor, now, body.reason)
        else:
            services.revert(s, pid, actor, now, body.reason)
        w = engine.load_world(s, now)
        p = w.proposal(pid)
        assert p is not None
        return views.proposal_detail(s, cfg, w, p)


@api.post("/proposals/{pid}/approve")
def approve(request: Request, pid: str, body: DecisionBody | None = None) -> dict[str, Any]:
    return _decide(request, pid, "approve", body or DecisionBody())


@api.post("/proposals/{pid}/reject")
def reject(request: Request, pid: str, body: DecisionBody | None = None) -> dict[str, Any]:
    return _decide(request, pid, "reject", body or DecisionBody())


@api.post("/proposals/{pid}/revert")
def revert(request: Request, pid: str, body: DecisionBody | None = None) -> dict[str, Any]:
    return _decide(request, pid, "revert", body or DecisionBody())


@api.get("/activity")
def activity(
    request: Request,
    actor: str = Query(default="all", pattern="^(all|agent|user)$"),
    type: str | None = Query(default=None, pattern="^(proposal|run|settings|guardrail)$"),
    subject_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    with _session(request) as s:
        stmt = select(AuditEvent)
        if actor != "all":
            stmt = stmt.where(AuditEvent.actor_type == actor)
        if type:
            stmt = stmt.where(AuditEvent.subject_type == type)
        if subject_id:
            stmt = stmt.where(AuditEvent.subject_id == subject_id)
        rows = s.exec(
            stmt.order_by(col(AuditEvent.at).desc(), col(AuditEvent.id).desc()).limit(limit)
        ).all()
        w = engine.load_world(s, utcnow())
        return {"items": [views.activity_json(e, views.titles(w)) for e in rows]}


@api.post("/runs", status_code=201)
async def start_run(request: Request, body: RunBody) -> dict[str, Any]:
    # async: the replay task must be created on the event loop, not in the threadpool
    with _session(request) as s:
        run = _start_run(request, s, body.type, "user")
        return views.run_summary(s, run, services.get_settings(s))


@api.get("/runs")
def list_runs(request: Request, limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    with _session(request) as s:
        settings = services.get_settings(s)
        rows = s.exec(select(Run).order_by(col(Run.started_at).desc()).limit(limit)).all()
        return {"items": [views.run_summary(s, r, settings) for r in rows]}


@api.get("/runs/{rid}")
def get_run(request: Request, rid: str) -> dict[str, Any]:
    with _session(request) as s:
        run = s.get(Run, rid)
        if run is None:
            raise services.NotFoundError(f"run {rid} not found")
        evs = s.exec(
            select(RunEvent).where(RunEvent.run_id == rid).order_by(col(RunEvent.seq))
        ).all()
        return {
            **views.run_summary(s, run, services.get_settings(s)),
            "events": [views.run_event_json(e) for e in evs],
        }


@api.get("/runs/{rid}/events")
async def run_events(request: Request, rid: str) -> StreamingResponse:
    db = _db(request)
    with Session(db) as s:
        if s.get(Run, rid) is None:
            raise services.NotFoundError(f"run {rid} not found")
    try:
        last = int(request.headers.get("last-event-id") or 0)
    except ValueError:
        last = 0

    async def stream() -> AsyncIterator[str]:
        nonlocal last
        idle = 0
        while True:
            with Session(db) as s:
                evs = s.exec(
                    select(RunEvent)
                    .where(RunEvent.run_id == rid, RunEvent.seq > last)
                    .order_by(col(RunEvent.seq))
                ).all()
                run = s.get(Run, rid)
                assert run is not None
                summary = views.run_summary(s, run, services.get_settings(s))
            for e in evs:
                last = e.seq
                yield sse("run_event", views.run_event_json(e), id=e.seq)
            if any(e.type == "end" for e in evs) or (run.status != "running" and not evs):
                yield sse("run", summary)
                return
            if await request.is_disconnected():
                return
            idle = 0 if evs else idle + 1
            if idle and idle % 60 == 0:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@api.post("/chat")
async def post_chat(request: Request, body: ChatBody) -> StreamingResponse:
    cfg = _cfg(request)
    with _session(request) as s:
        items = chat.events(
            engine.load_world(s, utcnow()), body.message, body.proposal_id, body.signal_id
        )
    speed = cfg.replay_speed

    async def stream() -> AsyncIterator[str]:
        for item in items:
            if speed > 0:
                if item["type"] == "tool":
                    await asyncio.sleep(item["duration_ms"] / 1000 / speed * 0.6)
                elif item["type"] == "token":
                    await asyncio.sleep(0.022 / speed)
            yield sse(item["type"], item)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@api.get("/settings")
def get_settings(request: Request) -> dict[str, Any]:
    cfg = _cfg(request)
    with _session(request) as s:
        return {
            "values": services.get_settings(s),
            "mode": cfg.mode,
            "apply_target": cfg.apply_target,
        }


@api.put("/settings")
def put_settings(request: Request, body: SettingsPatch) -> dict[str, Any]:
    cfg = _cfg(request)
    patch = body.model_dump(exclude_unset=True)
    if not cfg.is_fixture:
        patch.pop("fixture_verify_seconds", None)
        patch.pop("fixture_fail_next_verification", None)
    with _session(request) as s:
        values = services.update_settings(s, patch, _actor(s), utcnow())
        return {"values": values, "mode": cfg.mode, "apply_target": cfg.apply_target}


@api.get("/guardrails")
def list_guardrails(request: Request) -> dict[str, Any]:
    with _session(request) as s:
        w = engine.load_world(s, utcnow())
        items = sorted(w.guardrails, key=lambda g: (not g.builtin, g.created_at))
        return {"items": [views.guardrail_json(w, g) for g in items]}


@api.post("/guardrails", status_code=201)
def add_guardrail(request: Request, body: GuardrailBody) -> dict[str, Any]:
    with _session(request) as s:
        g = services.add_guardrail(s, body.model_dump(), _actor(s), utcnow())
        return views.guardrail_json(engine.load_world(s, utcnow()), g)


@api.delete("/guardrails/{gid}", status_code=204)
def delete_guardrail(request: Request, gid: str) -> None:
    with _session(request) as s:
        services.delete_guardrail(s, gid, _actor(s), utcnow())


@api.get("/connections")
def get_connections(request: Request) -> dict[str, Any]:
    return {"items": views.connections(_cfg(request))}


@api.post("/demo/reset")
async def demo_reset(request: Request) -> dict[str, str]:
    cfg = _cfg(request)
    if not cfg.is_fixture:
        raise services.ConflictError("demo reset is only available in fixture mode")
    await _cancel_runs(request.app)
    fixtures.seed(_db(request), cfg, utcnow())
    return {"status": "ok"}


# --- app --------------------------------------------------------------------------------


async def _cancel_runs(app: FastAPI) -> None:
    tasks = list(app.state.tasks)
    for t in tasks:
        t.cancel()
    for t in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t


async def _background(app: FastAPI) -> None:
    """Finishes due verifications and starts scheduled analysis runs."""
    db: Engine = app.state.db
    while True:
        await asyncio.sleep(1)
        try:
            with Session(db, expire_on_commit=False) as s:
                now = utcnow()
                services.run_due_verifications(s, now)
                settings = services.get_settings(s)
                every = int(settings.get("schedule_every_hours") or 0)
                if every and not settings.get("agent_paused") and runs.running_run(s) is None:
                    last = s.exec(select(Run).order_by(col(Run.started_at).desc())).first()
                    if last is None or (now - last.started_at).total_seconds() >= every * 3600:
                        run = runs.new_run(s, app.state.cfg, "analyze", "schedule", now)
                        _spawn(app, run.id)
        except Exception:  # noqa: BLE001 (keep the loop alive; the next tick retries)
            log.exception("background tick failed")


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.cfg = cfg
        app.state.db = make_engine(cfg.db_path)
        app.state.tasks = set()
        with Session(app.state.db) as s:
            if s.exec(select(Signal)).first() is None:
                fixtures.seed(app.state.db, cfg, utcnow())
            runs.fail_interrupted(s, utcnow())
        loop_task = (
            asyncio.get_running_loop().create_task(_background(app))
            if cfg.background_tasks
            else None
        )
        try:
            yield
        finally:
            if loop_task:
                loop_task.cancel()
            await _cancel_runs(app)
            app.state.db.dispose()

    app = FastAPI(
        title="Sift API",
        version=VERSION,
        lifespan=lifespan,
        description="Agent-first telemetry optimizer for Dynatrace",
    )

    @app.exception_handler(services.ConflictError)
    async def _conflict(_: Request, exc: services.ConflictError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(services.NotFoundError)
    async def _not_found(_: Request, exc: services.NotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    app.include_router(api)

    @app.get("/health")
    def root_health() -> dict[str, str]:
        return {"status": "ok", "version": VERSION, "mode": cfg.mode}

    if cfg.web_dist.is_dir():
        app.mount("/", StaticFiles(directory=cfg.web_dist, html=True), name="web")
    return app


app = create_app()
