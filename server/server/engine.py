"""Deterministic engine: volumes, cost, usage, guardrails, savings and dry runs.

Every number the UI or the agent shows is computed here from DB rows, so the overview,
the proposal rows and the agent's sentences always agree. Gemini never does arithmetic."""

from __future__ import annotations

import fnmatch
import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlmodel import Session, select

from server import fmt
from server.engines.cost import CostModel
from server.models import Guardrail, Proposal, Reference, Setting, Signal

GIB = 1024**3
DAYS_PER_MONTH = 30
ACTIVE = ("applied", "verifying", "verified")
STRONG_REF_TYPES = ("dashboard", "notebook", "workflow", "alert", "slo")
GUARD_REF_TYPES = ("alert", "slo", "workflow")
NOUN = {"logs": "log records", "spans": "spans", "metrics": "data points"}
# Actions that remove data. Guardrails block these; `redact` only masks.
REMOVING_ACTIONS = ("drop", "sample", "drop_field", "logs_to_metric", "lower_level")


@dataclass
class World:
    """A consistent snapshot of everything the engine needs, loaded per request."""

    now: datetime
    signals: dict[str, Signal]
    refs: dict[str, list[Reference]]
    proposals: list[Proposal]
    guardrails: list[Guardrail]
    settings: dict[str, Any]
    children: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def __post_init__(self) -> None:
        for sig in self.signals.values():
            if sig.parent_id:
                self.children[sig.parent_id].append(sig.id)

    def rate(self, data_type: str) -> float:
        return float(self.settings["rates_eur_per_gib"][data_type])

    @property
    def cost_model(self) -> CostModel:
        return CostModel.from_settings(self.settings)

    def unattributed(self) -> dict[str, float]:
        """GiB/day of billed data without signals (events), written by discovery."""
        return {k: float(v) for k, v in (self.settings.get("unattributed_gib_day") or {}).items()}

    def proposal(self, pid: str) -> Proposal | None:
        return next((p for p in self.proposals if p.id == pid), None)


def load_world(session: Session, now: datetime) -> World:
    refs: dict[str, list[Reference]] = defaultdict(list)
    for r in session.exec(select(Reference)).all():
        refs[r.signal_id].append(r)
    return World(
        now=now,
        signals={s.id: s for s in session.exec(select(Signal)).all()},
        refs=refs,
        proposals=list(session.exec(select(Proposal)).all()),
        guardrails=list(session.exec(select(Guardrail)).all()),
        settings={s.key: s.value for s in session.exec(select(Setting)).all()},
    )


# --- volume -----------------------------------------------------------------------------


def records_day(sig: Signal, gib_day: float | None = None) -> float:
    return (sig.gib_day if gib_day is None else gib_day) * GIB / sig.avg_record_bytes


def descendants(world: World, sid: str) -> list[str]:
    out: list[str] = []
    for child in world.children.get(sid, []):
        out.append(child)
        out.extend(descendants(world, child))
    return out


def ancestors(world: World, sig: Signal) -> list[Signal]:
    out: list[Signal] = []
    cur = world.signals.get(sig.parent_id) if sig.parent_id else None
    while cur is not None:
        out.append(cur)
        cur = world.signals.get(cur.parent_id) if cur.parent_id else None
    return out


def active_interval(p: Proposal) -> tuple[datetime, datetime | None] | None:
    """When the proposal's rule was or is in effect."""
    if p.applied_at is None:
        return None
    if p.status in ACTIVE:
        return (p.applied_at, None)
    if p.status == "reverted" and p.reverted_at is not None:
        return (p.applied_at, p.reverted_at)
    return None


def _noise(key: str, day: date) -> float:
    """Deterministic ±4 % day-to-day wobble so charts look like real ingest."""
    h = hashlib.sha256(f"{key}:{day.isoformat()}".encode()).digest()
    return 1 + (h[0] / 255 - 0.5) * 0.08


def _baseline(sig: Signal, day: date, today: date) -> float:
    days_ago = (today - day).days
    return sig.gib_day * (1 + sig.growth_wow) ** (-days_ago / 7) * _noise(sig.id, day)


def _overlap(interval: tuple[datetime, datetime | None], start: datetime, end: datetime) -> float:
    lo = max(interval[0], start)
    hi = min(interval[1] or end, end)
    return max(0.0, (hi - lo).total_seconds()) / (end - start).total_seconds()


def _rules_on(world: World, sig: Signal) -> list[Proposal]:
    ids = {sig.id, *descendants(world, sig.id)}
    return [p for p in world.proposals if p.signal_id in ids and active_interval(p)]


def daily_series(world: World, sig: Signal, days: int = 14) -> list[tuple[date, float]]:
    """GiB per day, oldest first. Today is projected to a full day."""
    today = world.now.date()
    rules = _rules_on(world, sig)
    out: list[tuple[date, float]] = []
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        start = datetime.combine(day, time(), tzinfo=UTC)
        end = start + timedelta(days=1)
        value = _baseline(sig, day, today)
        for p in rules:
            target = world.signals[p.signal_id]
            interval = active_interval(p)
            assert interval is not None
            value -= (
                _baseline(target, day, today) * p.reduction_ratio * _overlap(interval, start, end)
            )
        out.append((day, max(value, 0.0)))
    return out


def current_gib_day(world: World, sig: Signal) -> float:
    """Today's rate: the baseline minus every rule active right now."""
    removed = 0.0
    for p in _rules_on(world, sig):
        interval = active_interval(p)
        if interval and interval[1] is None:
            removed += world.signals[p.signal_id].gib_day * p.reduction_ratio
    return max(sig.gib_day - removed, 0.0)


def cost_month(world: World, sig: Signal, gib_day: float | None = None) -> float:
    g = current_gib_day(world, sig) if gib_day is None else gib_day
    return world.cost_model.monthly(sig.data_type, g)


def partition(world: World) -> list[Signal]:
    """Top-level signals. Children (patterns, dimensions, attributes) are subsets of these."""
    return [s for s in world.signals.values() if s.parent_id is None]


# --- savings ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Savings:
    gib_day: float
    gib_month: float
    eur_month: float


def savings(world: World, p: Proposal) -> Savings:
    target = world.signals[p.signal_id]
    removed = target.gib_day * p.reduction_ratio
    gib_day = removed - p.added_gib_day
    model = world.cost_model
    # all_in mode: the same for every apply target. split mode: per target (see cost.py).
    eur = model.saving(target.data_type, removed) - model.monthly("metrics", p.added_gib_day)
    return Savings(gib_day=gib_day, gib_month=gib_day * DAYS_PER_MONTH, eur_month=eur)


# --- usage ------------------------------------------------------------------------------


def affecting_refs(world: World, sig: Signal) -> list[Reference]:
    """References that read the data this signal represents.

    A query on a log source also returns its patterns, so patterns inherit every parent
    reference. A dimension or attribute is only affected where a reference uses that field."""
    refs = list(world.refs.get(sig.id, []))
    for parent in ancestors(world, sig):
        for r in world.refs.get(parent.id, []):
            if sig.kind in ("metric_dimension", "span_attribute"):
                if sig.key in r.fields or "*" in r.fields:  # "*": raw records, every field
                    refs.append(r)
            else:
                refs.append(r)
    return refs


def related_refs(world: World, sig: Signal) -> list[Reference]:
    """Dashboards and alerts next to the signal that a rule on it leaves untouched."""
    affecting = {r.id for r in affecting_refs(world, sig)}
    pool: list[Reference] = []
    for parent in ancestors(world, sig):
        pool.extend(world.refs.get(parent.id, []))
    if sig.parent_id:
        siblings = [c for c in world.children[sig.parent_id] if c != sig.id]
    else:
        siblings = [
            s.id
            for s in partition(world)
            if s.id != sig.id and s.service == sig.service and s.data_type == sig.data_type
        ]
    for sid in siblings:
        pool.extend(world.refs.get(sid, []))
    seen: set[tuple[str, str]] = set()
    out: list[Reference] = []
    for r in pool:
        key = (r.type, r.name)
        if r.id in affecting or r.type not in STRONG_REF_TYPES or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _recent(r: Reference, now: datetime, days: int) -> bool:
    return r.last_used is not None and now - r.last_used <= timedelta(days=days)


@dataclass(frozen=True)
class Usage:
    klass: str  # used | rarely | unused
    queries_30d: int
    by_type: dict[str, int]  # strong references used in the last 30 days, by type
    last_used: datetime | None
    references_30d: int


def usage(world: World, sig: Signal) -> Usage:
    refs = affecting_refs(world, sig)
    by_type: dict[str, int] = defaultdict(int)
    queries = 0
    for r in refs:
        if r.type == "query":
            queries += r.count_30d
        elif _recent(r, world.now, 30):
            by_type[r.type] += 1
    last = max((r.last_used for r in refs if r.last_used), default=None)
    strong = sum(by_type.values())
    if strong or queries >= 10:
        klass = "used"
    elif queries > 0:
        klass = "rarely"
    else:
        klass = "unused"
    refs_30d = strong + sum(1 for r in refs if r.type == "query" and r.count_30d > 0)
    return Usage(klass, queries, dict(by_type), last, refs_30d)


# --- guardrails -------------------------------------------------------------------------


def _level(world: World, sig: Signal) -> str | None:
    if sig.level:
        return sig.level
    return next((a.level for a in ancestors(world, sig) if a.level), None)


def _source(world: World, sig: Signal) -> str:
    return sig.source or next((a.source for a in ancestors(world, sig) if a.source), "") or ""


def guardrail_hits(world: World, sig: Signal) -> list[Guardrail]:
    hits: list[Guardrail] = []
    for g in world.guardrails:
        if g.builtin:
            if g.rule == "error_levels":
                hit = sig.data_type == "logs" and _level(world, sig) in ("ERROR", "FATAL")
            elif g.rule == "audit_logs":
                hit = sig.data_type == "logs" and "audit" in _source(world, sig).lower()
            elif g.rule == "referenced_90d":
                hit = any(
                    r.type in GUARD_REF_TYPES and _recent(r, world.now, 90)
                    for r in affecting_refs(world, sig)
                )
            else:
                hit = False
        else:
            hit = (
                (g.service is None or g.service == sig.service)
                and (g.kind is None or g.kind == sig.kind)
                and (g.key_pattern is None or fnmatch.fnmatchcase(sig.key, g.key_pattern))
            )
        if hit:
            hits.append(g)
    return hits


def blocking_guardrails(world: World, p: Proposal) -> list[Guardrail]:
    if p.action not in REMOVING_ACTIONS:
        return []
    return guardrail_hits(world, world.signals[p.signal_id])


# --- status -----------------------------------------------------------------------------


def proposal_for(world: World, sig: Signal) -> Proposal | None:
    """The proposal that currently defines this signal's state, if any."""
    order = ("awaiting_approval", "verifying", "applied", "verified")
    for status in order:
        for p in world.proposals:
            if p.signal_id == sig.id and p.status == status:
                return p
    return None


def signal_status(world: World, sig: Signal) -> str:
    p = proposal_for(world, sig)
    if p is not None:
        return p.status
    if guardrail_hits(world, sig):
        return "protected"
    return usage(world, sig).klass


# --- dry run ----------------------------------------------------------------------------


def _scope_signals(world: World, scope: dict[str, Any]) -> list[Signal]:
    if "signal" in scope:
        return [world.signals[scope["signal"]]]
    return [
        s
        for s in partition(world)
        if s.service == scope["service"] and s.data_type == scope["data_type"]
    ]


def scope_label(world: World, scope: dict[str, Any]) -> str:
    """'payment log records', 'frontend spans', 'http.server.request.duration data points'."""
    if "signal" in scope:
        sig = world.signals[scope["signal"]]
        return f"{sig.key} {NOUN[sig.data_type]}"
    return f"{scope['service']} {NOUN[scope['data_type']]}"


def scope_volume_label(world: World, scope: dict[str, Any]) -> str:
    """'payment log volume', 'frontend span volume', 'http.server.request.duration volume'."""
    if "signal" in scope:
        return f"{world.signals[scope['signal']].key} volume"
    noun = {"logs": "log", "spans": "span", "metrics": "metric"}[scope["data_type"]]
    return f"{scope['service']} {noun} volume"


@dataclass(frozen=True)
class DryRun:
    matched_records_day: float
    scope_records_day: float
    scope_label: str
    match_ratio: float
    errors_matched: float
    share_of_scope_bytes: float


def dry_run(world: World, p: Proposal) -> DryRun:
    """Fixture dry run. Live mode runs the matcher as a DQL filter over the last 24 h."""
    target = world.signals[p.signal_id]
    scope = _scope_signals(world, p.scope)
    scope_records = sum(records_day(s) for s in scope)
    scope_gib = sum(s.gib_day for s in scope)
    ratio = p.reduction_ratio if p.action == "drop" else 1.0
    matched = records_day(target) * ratio
    errors = matched if _level(world, target) in ("ERROR", "FATAL") else 0.0
    return DryRun(
        matched_records_day=matched,
        scope_records_day=scope_records,
        scope_label=scope_label(world, p.scope),
        match_ratio=matched / scope_records if scope_records else 0.0,
        errors_matched=errors,
        share_of_scope_bytes=target.gib_day / scope_gib if scope_gib else 0.0,
    )


# --- text -------------------------------------------------------------------------------


class _Strict(dict):  # type: ignore[type-arg]
    def __missing__(self, key: str) -> str:
        raise KeyError(f"unknown placeholder {{{key}}}")


def signal_context(world: World, sig: Signal) -> dict[str, str]:
    u = usage(world, sig)
    return {
        "service": sig.service,
        "key": sig.key,
        "gib_day": fmt.gib(sig.gib_day),
        "records_day": fmt.count(records_day(sig)),
        "gib_month": fmt.gib(sig.gib_day * DAYS_PER_MONTH),
        "eur_month": fmt.eur(world.cost_model.monthly(sig.data_type, sig.gib_day)),
        "last_used": fmt.ago(u.last_used, world.now),
        "queries_30d": f"{u.queries_30d}",
    }


def proposal_context(world: World, p: Proposal) -> dict[str, str]:
    target = world.signals[p.signal_id]
    s = savings(world, p)
    d = dry_run(world, p)
    ctx = signal_context(world, target)
    ctx.update(
        {
            "save_gib_month": fmt.gib(s.gib_month),
            "save_gib_week": fmt.gib(s.gib_day * 7),
            "save_eur_month": fmt.eur(s.eur_month),
            "matched": fmt.count(d.matched_records_day),
            "scope_records": fmt.count(d.scope_records_day),
            "scope_label": d.scope_label,
            "match_pct": fmt.pct(d.match_ratio),
            "share": fmt.pct(d.share_of_scope_bytes),
            "errors_matched": fmt.count(d.errors_matched),
        }
    )
    return ctx


def render(template: str, ctx: dict[str, str]) -> str:
    return template.format_map(_Strict(ctx))
