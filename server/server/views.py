"""JSON views for the API. Shapes mirror web/src/types.ts; keep the two in sync."""

from __future__ import annotations

import os
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlmodel import Session, col, select

from server import engine, fixtures, fmt
from server.config import Config
from server.engine import World
from server.models import AuditEvent, Guardrail, Proposal, Reference, Run, RunEvent, Signal

STATUS_GROUPS: dict[str, tuple[str, ...]] = {
    "awaiting": ("awaiting_approval",),
    "applied": ("approved", "applied", "verifying"),
    "verified": ("verified",),
    "reverted": ("reverted",),
    "rejected": ("rejected",),
}


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _r(x: float, nd: int = 4) -> float:
    return round(x, nd)


# --- signals ----------------------------------------------------------------------------


def usage_json(u: engine.Usage) -> dict[str, Any]:
    return {
        "klass": u.klass,
        "queries_30d": u.queries_30d,
        "by_type": u.by_type,
        "last_used": iso(u.last_used),
        "references_30d": u.references_30d,
    }


def signal_summary(w: World, sig: Signal) -> dict[str, Any]:
    current = engine.current_gib_day(w, sig)
    p = engine.proposal_for(w, sig)
    return {
        "id": sig.id,
        "kind": sig.kind,
        "data_type": sig.data_type,
        "service": sig.service,
        "key": sig.key,
        "parent_id": sig.parent_id,
        "level": sig.level or next((a.level for a in engine.ancestors(w, sig) if a.level), None),
        "gib_day": _r(current),
        "baseline_gib_day": _r(sig.gib_day),
        "records_day": round(engine.records_day(sig, current)),
        "cost_month": _r(engine.cost_month(w, sig, current), 2),
        "usage": usage_json(engine.usage(w, sig)),
        "status": engine.signal_status(w, sig),
        "protected_by": [g.name for g in engine.guardrail_hits(w, sig)],
        "proposal_id": p.id if p else None,
        "finding_class": sig.finding_class,
    }


def series_json(w: World, sig: Signal, days: int = 14) -> list[dict[str, Any]]:
    return [{"date": d.isoformat(), "gib": _r(v)} for d, v in engine.daily_series(w, sig, days)]


def reference_json(w: World, r: Reference, effect: str, note: str | None) -> dict[str, Any]:
    owner_sig = w.signals.get(r.signal_id)
    return {
        "id": r.id,
        "type": r.type,
        "name": r.name,
        "owner": r.owner,
        "count_30d": r.count_30d,
        "last_used": iso(r.last_used),
        "fields": r.fields,
        "dql": r.dql,
        "note": r.note,
        "signal_id": r.signal_id,
        "signal_label": f"{owner_sig.service} · {owner_sig.key}" if owner_sig else r.signal_id,
        "effect": effect,  # reads | not_affected
        "effect_note": note,
    }


def impact(w: World, sig: Signal) -> list[dict[str, Any]]:
    rows = [reference_json(w, r, "reads", None) for r in engine.affecting_refs(w, sig)]
    for r in engine.related_refs(w, sig):
        owner = w.signals[r.signal_id]
        if r.fields and sig.kind in ("metric_dimension", "span_attribute"):
            note = f"Reads {', '.join(r.fields)}, not {sig.key}"
        else:
            note = f"Reads {owner.key}, not this data"
        rows.append(reference_json(w, r, "not_affected", note))
    return rows


def signal_detail(w: World, sig: Signal) -> dict[str, Any]:
    ctx = engine.signal_context(w, sig)
    note = engine.render(sig.finding_note, ctx) if sig.finding_note else None
    return {
        **signal_summary(w, sig),
        "first_seen": iso(sig.first_seen),
        "avg_record_bytes": sig.avg_record_bytes,
        "series": series_json(w, sig),
        "markers": _markers(w, [sig.id, *engine.descendants(w, sig.id)]),
        "samples": sig.samples,
        "finding": {"klass": sig.finding_class, "note": note} if sig.finding_class else None,
        "references": impact(w, sig),
        "proposals": [proposal_summary(w, p) for p in w.proposals if p.signal_id == sig.id],
        "parent": signal_summary(w, w.signals[sig.parent_id]) if sig.parent_id else None,
        "children": [signal_summary(w, w.signals[c]) for c in w.children.get(sig.id, [])],
    }


def _markers(w: World, signal_ids: list[str]) -> list[dict[str, Any]]:
    """Dashed lines on the volume chart: when a rule was applied (and reverted)."""
    out = []
    for p in w.proposals:
        if p.signal_id in signal_ids and p.applied_at:
            out.append(
                {
                    "date": p.applied_at.date().isoformat(),
                    "label": "Rule applied",
                    "proposal_id": p.id,
                }
            )
            if p.reverted_at:
                out.append(
                    {
                        "date": p.reverted_at.date().isoformat(),
                        "label": "Rule reverted",
                        "proposal_id": p.id,
                    }
                )
    return out


# --- proposals --------------------------------------------------------------------------


def proposal_summary(w: World, p: Proposal) -> dict[str, Any]:
    sig = w.signals[p.signal_id]
    s = engine.savings(w, p)
    u = engine.usage(w, sig)
    return {
        "id": p.id,
        "title": p.title,
        "action": p.action,
        "kind": sig.kind,
        "data_type": sig.data_type,
        "service": sig.service,
        "signal_id": sig.id,
        "signal_key": sig.key,
        "status": p.status,
        "confidence": p.confidence,
        "finding_class": p.finding_class,
        "savings": {
            "gib_day": _r(s.gib_day),
            "gib_week": _r(s.gib_day * 7),
            "gib_month": _r(s.gib_month),
            "eur_month": _r(s.eur_month, 2),
        },
        "usage": usage_json(u),
        "created_at": iso(p.created_at),
        "decided_by": p.decided_by,
        "decided_at": iso(p.decided_at),
        "applied_at": iso(p.applied_at),
        "verify_due_at": iso(p.verify_due_at),
        "verified_at": iso(p.verified_at),
        "reverted_at": iso(p.reverted_at),
    }


def blast_radius(w: World, sig: Signal) -> dict[str, int]:
    counts = {t: 0 for t in ("dashboard", "notebook", "workflow", "alert", "slo", "query")}
    for r in engine.affecting_refs(w, sig):
        if r.type == "query":
            counts["query"] += r.count_30d
        elif r.last_used and w.now - r.last_used <= timedelta(days=90):
            counts[r.type] += 1
    return counts


def _evidence(w: World, p: Proposal) -> list[dict[str, Any]]:
    sig = w.signals[p.signal_id]
    d = engine.dry_run(w, p)
    u = engine.usage(w, sig)
    blocking = engine.blocking_guardrails(w, p)
    hits = engine.guardrail_hits(w, sig)
    if u.references_30d:
        usage_text = (
            f"{u.references_30d} references read this data in the last 30 days. "
            f"Last read {fmt.ago(u.last_used, w.now)}."
        )
    else:
        usage_text = f"No references in the last 30 days. Last read {fmt.ago(u.last_used, w.now)}."
    if blocking:
        guard_text = "Blocked by " + ", ".join(g.name for g in blocking) + "."
    elif hits:
        guard_text = (
            "Protected by "
            + ", ".join(g.name for g in hits)
            + f", but {p.action} only masks values, so it is allowed."
        )
    else:
        guard_text = f"No guardrail matches. {len(w.guardrails)} guardrails checked."
    return [
        {
            "n": 1,
            "label": "Volume",
            "target": "volume",
            "text": f"{fmt.gib(sig.gib_day)} a day, {fmt.count(engine.records_day(sig))} records, "
            f"{fmt.pct(d.share_of_scope_bytes)} of {engine.scope_volume_label(w, p.scope)}.",
        },
        {"n": 2, "label": "Usage", "target": "impact", "text": usage_text},
        {
            "n": 3,
            "label": "Dry run",
            "target": "rule",
            "text": f"Matcher hits {fmt.count(d.matched_records_day)} of "
            f"{fmt.count(d.scope_records_day)} {d.scope_label} a day "
            f"({fmt.pct(d.match_ratio)}). {fmt.count(d.errors_matched)} ERROR or FATAL.",
        },
        {"n": 4, "label": "Guardrails", "target": "guardrails", "text": guard_text},
    ]


def proposal_detail(session: Session, cfg: Config, w: World, p: Proposal) -> dict[str, Any]:
    sig = w.signals[p.signal_id]
    ctx = engine.proposal_context(w, p)
    d = engine.dry_run(w, p)
    u = engine.usage(w, sig)
    history = session.exec(
        select(AuditEvent)
        .where(AuditEvent.subject_type == "proposal", AuditEvent.subject_id == p.id)
        .order_by(col(AuditEvent.at), col(AuditEvent.id))
    ).all()
    return {
        **proposal_summary(w, p),
        "explanation": [engine.render(t, ctx) for t in p.explanation],
        "evidence": _evidence(w, p),
        "kpis": {
            "gib_day": _r(sig.gib_day),
            "records_day": round(engine.records_day(sig)),
            "cost_month": _r(sig.gib_day * engine.DAYS_PER_MONTH * w.rate(sig.data_type), 2),
            "last_used": iso(u.last_used),
            "references_30d": u.references_30d,
        },
        "series": series_json(w, sig),
        "markers": _markers(w, [sig.id]),
        "samples": sig.samples,
        "rule": p.rule,
        "dry_run": {
            "matched_records_day": round(d.matched_records_day),
            "scope_records_day": round(d.scope_records_day),
            "scope_label": d.scope_label,
            "match_ratio": _r(d.match_ratio),
            "errors_matched": round(d.errors_matched),
        },
        "impact": impact(w, sig),
        "blast_radius": blast_radius(w, sig),
        "blocking_guardrails": [g.name for g in engine.blocking_guardrails(w, p)],
        "history": [activity_json(e, {p.id: p.title}) for e in history],
        "verify_result": p.verify_result,
        "revert_reason": p.revert_reason,
        "reject_reason": p.reject_reason,
        "run_id": p.run_id,
        "apply_target": cfg.apply_target,
        "signal": signal_summary(w, sig),
    }


# --- activity ---------------------------------------------------------------------------


def activity_json(e: AuditEvent, titles: dict[str, str]) -> dict[str, Any]:
    return {
        "id": e.id,
        "at": iso(e.at),
        "actor": e.actor,
        "actor_type": e.actor_type,
        "action": e.action,
        "subject_type": e.subject_type,
        "subject_id": e.subject_id,
        "subject_title": titles.get(e.subject_id or ""),
        "summary": e.summary,
        "before": e.before,
        "after": e.after,
    }


# --- runs -------------------------------------------------------------------------------


def run_summary(session: Session, run: Run, settings: dict[str, Any]) -> dict[str, Any]:
    current = session.exec(
        select(RunEvent)
        .where(RunEvent.run_id == run.id, RunEvent.type == "step")
        .order_by(col(RunEvent.seq).desc())
    ).first()
    rates = settings.get("gemini_usd_per_m_tokens") or {}
    cost = None
    if rates.get("input") is not None and rates.get("output") is not None:
        cost = round(
            run.tokens_in / 1e6 * rates["input"] + run.tokens_out / 1e6 * rates["output"], 4
        )
    return {
        "id": run.id,
        "type": run.type,
        "status": run.status,
        "trigger": run.trigger,
        "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at),
        "summary": run.summary,
        "current_step": current.label if current else None,
        "requested_model": run.requested_model,
        "served_model": run.served_model,
        "fallback_reason": run.fallback_reason,
        "tokens_in": run.tokens_in,
        "tokens_out": run.tokens_out,
        "est_cost_usd": cost,
        "scripted": run.scripted,
    }


def run_event_json(e: RunEvent) -> dict[str, Any]:
    return {
        "seq": e.seq,
        "at": iso(e.at),
        "type": e.type,
        "label": e.label,
        "status": e.status,
        "duration_ms": e.duration_ms,
        "tool": e.tool,
        "rows": e.rows,
        "result": e.result,
    }


# --- overview ---------------------------------------------------------------------------


def _ingest_week(w: World, cfg: Config, weeks_ago: int) -> tuple[dict[str, float], float]:
    """GiB per data type in one 7-day window (0 = the last 7 days including today)."""
    by_type = {"logs": 0.0, "spans": 0.0, "metrics": 0.0}
    for sig in engine.partition(w):
        series = engine.daily_series(w, sig, 7 * (weeks_ago + 1))
        by_type[sig.data_type] += sum(v for _, v in series[:7])
    events = sum(fixtures.unattributed_gib_day(cfg).values()) * 7
    return by_type, sum(by_type.values()) + events


def overview(session: Session, cfg: Config, w: World) -> dict[str, Any]:
    this_week, total = _ingest_week(w, cfg, 0)
    _, prev_total = _ingest_week(w, cfg, 1)
    unattributed = fixtures.unattributed_gib_day(cfg)
    cost = sum(engine.cost_month(w, s) for s in engine.partition(w))
    cost += sum(v * engine.DAYS_PER_MONTH * w.rate(k) for k, v in unattributed.items())

    awaiting = sorted(
        (p for p in w.proposals if p.status == "awaiting_approval"),
        key=lambda p: -engine.savings(w, p).eur_month,
    )
    verified = [p for p in w.proposals if p.status == "verified"]
    waste_week = sum(engine.savings(w, p).gib_day * 7 for p in awaiting)
    failed = sorted(
        (
            p
            for p in w.proposals
            if p.status == "reverted"
            and p.verify_result
            and not p.verify_result.get("passed", True)
        ),
        key=lambda p: p.reverted_at or w.now,
        reverse=True,
    )

    running = session.exec(select(Run).where(Run.status == "running")).first()
    last = session.exec(
        select(Run).where(Run.status != "running").order_by(col(Run.started_at).desc())
    ).first()
    paused = bool(w.settings.get("agent_paused"))
    next_run = next_run_at(w, last)
    today = datetime.combine(w.now.date(), time(), tzinfo=UTC)
    tokens_today = sum(
        r.tokens_in + r.tokens_out
        for r in session.exec(select(Run).where(Run.started_at >= today)).all()
    )
    composition = [
        {"key": "logs", "label": "Logs", "gib": _r(this_week["logs"], 2)},
        {"key": "spans", "label": "Spans", "gib": _r(this_week["spans"], 2)},
        {"key": "metrics", "label": "Metrics", "gib": _r(this_week["metrics"], 2)},
        {"key": "events", "label": "Events", "gib": _r(unattributed.get("events", 0) * 7, 2)},
    ]
    return {
        "now": iso(w.now),
        "kpis": {
            "ingest_7d_gib": _r(total, 2),
            "ingest_prev_7d_gib": _r(prev_total, 2),
            "cost_month_eur": _r(cost, 2),
            "waste_gib_week": _r(waste_week, 2),
            "waste_ratio": _r(waste_week / total if total else 0.0),
            "waste_eur_month": _r(sum(engine.savings(w, p).eur_month for p in awaiting), 2),
            "awaiting_count": len(awaiting),
            "saved_eur_month": _r(sum(engine.savings(w, p).eur_month for p in verified), 2),
            "verified_count": len(verified),
        },
        "awaiting": [proposal_summary(w, p) for p in awaiting[:3]],
        "composition": composition,
        "failed_verifications": {
            "count": len(failed),
            "latest_proposal_id": failed[0].id if failed else None,
        },
        "agent": {
            "state": "running" if running else ("paused" if paused else "idle"),
            "run": run_summary(session, running, w.settings) if running else None,
            "last_run": run_summary(session, last, w.settings) if last else None,
            "next_run_at": iso(next_run),
            "verify_window_min": int(w.settings.get("verify_window_min", cfg.verify_window_min)),
            "fixture_verify_seconds": float(w.settings.get("fixture_verify_seconds", 0))
            if cfg.is_fixture
            else None,
        },
        "footer": {
            "signals": len(w.signals),
            "query_history_days": 30,
            "gemini_tokens_today": tokens_today,
            "scripted": cfg.is_fixture,
        },
    }


# --- guardrails and connections ---------------------------------------------------------


def guardrail_json(w: World, g: Guardrail) -> dict[str, Any]:
    single = World(
        now=w.now,
        signals=w.signals,
        refs=w.refs,
        proposals=w.proposals,
        guardrails=[g],
        settings=w.settings,
    )
    matches = [s for s in w.signals.values() if engine.guardrail_hits(single, s)]
    return {
        "id": g.id,
        "builtin": g.builtin,
        "name": g.name,
        "reason": g.reason,
        "rule": g.rule,
        "service": g.service,
        "kind": g.kind,
        "key_pattern": g.key_pattern,
        "created_by": g.created_by,
        "created_at": iso(g.created_at),
        "matches": [
            {
                "id": s.id,
                "service": s.service,
                "key": s.key,
                "kind": s.kind,
                "gib_day": _r(s.gib_day),
                "cost_month": _r(engine.cost_month(w, s), 2),
            }
            for s in sorted(matches, key=lambda s: -s.gib_day)
        ],
    }


def connections(cfg: Config) -> list[dict[str, Any]]:
    def present(name: str) -> str:
        return "set" if os.getenv(name) else "not set"

    fixture = cfg.is_fixture
    return [
        {
            "id": "dynatrace",
            "name": "Dynatrace environment",
            "status": "fixture" if fixture else "connected",
            "summary": f"{cfg.env_id} · recorded data from fixtures/"
            if fixture
            else cfg.platform_url,
            "description": "Sift reads ingest volume, query executions, dashboards, notebooks, "
            "workflows and alerts from Grail.",
            "fields": [
                {"label": "Mode", "value": "fixture (SIFT_MODE)"},
                {"label": "Environment", "value": cfg.env_id},
                {"label": "Platform token", "value": present("DT_PLATFORM_TOKEN")},
            ],
            "note": "Live mode arrives in phase 02. Token scopes are verified there."
            if fixture
            else None,
        },
        {
            "id": "dynatrace-mcp",
            "name": "Dynatrace MCP server",
            "status": "fixture"
            if fixture
            else ("connected" if os.getenv("DT_MCP_URL") else "not_configured"),
            "summary": "Not used in fixture mode" if fixture else os.getenv("DT_MCP_URL", ""),
            "description": "The agent uses it to run DQL, read problems and look up entities.",
            "fields": [{"label": "DT_MCP_URL", "value": present("DT_MCP_URL")}],
            "note": None,
        },
        {
            "id": "vertex",
            "name": "Gemini on Vertex AI",
            "status": "fixture" if fixture else "connected",
            "summary": "Scripted runs, no Gemini calls"
            if fixture
            else os.getenv("GOOGLE_CLOUD_PROJECT", ""),
            "description": "Every LLM call goes to Gemini on Vertex AI, billed to the hackathon "
            "project. Calls fall back to the fast model on 429.",
            "fields": [
                {"label": "GOOGLE_CLOUD_PROJECT", "value": present("GOOGLE_CLOUD_PROJECT")},
                {"label": "GEMINI_MODEL", "value": os.getenv("GEMINI_MODEL") or "not set"},
                {
                    "label": "GEMINI_FAST_MODEL",
                    "value": os.getenv("GEMINI_FAST_MODEL") or "not set",
                },
            ],
            "note": "Fixture mode shows illustrative token counts from fixtures/runs.json."
            if fixture
            else None,
        },
        {
            "id": "apply",
            "name": "Apply target",
            "status": "dry_run" if cfg.apply_target == "dry-run" or fixture else "connected",
            "summary": {
                "dry-run": "Dry run: rules are never written",
                "openpipeline": "OpenPipeline configuration",
                "otelcol": "OpenTelemetry Collector config",
            }[cfg.apply_target],
            "description": "Where an approved rule goes. Sift stores the previous state first "
            "so it can revert.",
            "fields": [{"label": "SIFT_APPLY_TARGET", "value": cfg.apply_target}],
            "note": "Fixture mode never writes, whatever the target." if fixture else None,
        },
    ]


def titles(w: World) -> dict[str, str]:
    out = {p.id: p.title for p in w.proposals}
    out.update({g.id: g.name for g in w.guardrails})
    return out


def next_run_at(w: World, last: Run | None) -> datetime | None:
    """The scheduler runs analysis every N hours after the last run (0 = manual only)."""
    every = int(w.settings.get("schedule_every_hours") or 0)
    if not every or w.settings.get("agent_paused"):
        return None
    step = timedelta(hours=every)
    nxt = (last.started_at if last else w.now) + step
    while nxt <= w.now:
        nxt += step
    return nxt
