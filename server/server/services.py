"""State changes: proposals, verification, settings and guardrails. Every change writes an
AuditEvent with the actor, before and after."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from server import engine, fmt
from server.config import Config
from server.models import AuditEvent, Guardrail, Proposal, Setting


class ConflictError(Exception):
    """The requested change is not allowed in the current state (HTTP 409)."""


class NotFoundError(Exception):
    """HTTP 404."""


def audit(
    session: Session,
    at: datetime,
    actor: str,
    action: str,
    subject_type: str,
    subject_id: str | None,
    summary: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            at=at,
            actor=actor,
            actor_type="agent" if actor == "agent" else "user",
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            summary=summary,
            before=before,
            after=after,
        )
    )


# --- settings ---------------------------------------------------------------------------

SETTING_LABELS: dict[str, str] = {
    "user_name": "your name",
    "user_role": "your role",
    "rates_eur_per_gib.logs": "the log rate",
    "rates_eur_per_gib.spans": "the span rate",
    "rates_eur_per_gib.metrics": "the metric rate",
    "rates_eur_per_gib.events": "the event rate",
    "gemini_usd_per_m_tokens.input": "the Gemini input-token rate",
    "gemini_usd_per_m_tokens.output": "the Gemini output-token rate",
    "verify_window_min": "the verification window",
    "schedule_every_hours": "the analysis schedule",
    "agent_paused": "agent pause",
    "min_savings_eur_month": "the minimum saving per proposal",
    "fixture_verify_seconds": "the fixture verification delay",
    "fixture_fail_next_verification": "the simulated verification failure",
}


def get_settings(session: Session) -> dict[str, Any]:
    return {s.key: s.value for s in session.exec(select(Setting)).all()}


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, f"{key}."))
        else:
            out[key] = v
    return out


def _describe(key: str, value: Any) -> str:
    if value is None:
        return "not set"
    if key.startswith("rates_eur_per_gib."):
        return f"€{value:.2f} per GiB"
    if key.startswith("gemini_usd_per_m_tokens."):
        return f"${value:.2f} per 1M tokens"
    if key == "verify_window_min":
        return f"{value} min"
    if key == "schedule_every_hours":
        return "manual" if value == 0 else f"every {value} h"
    if key == "fixture_verify_seconds":
        return f"{value} s"
    if key == "min_savings_eur_month":
        return f"€{value} a month"
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def update_settings(
    session: Session, patch: dict[str, Any], actor: str, now: datetime
) -> dict[str, Any]:
    current = get_settings(session)
    merged = copy.deepcopy(current)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    before_flat, after_flat = _flatten(current), _flatten(merged)
    changed = [k for k in after_flat if after_flat[k] != before_flat.get(k)]
    for key in changed:
        top = key.split(".", 1)[0]
        row = session.get(Setting, top)
        if row is None:
            session.add(Setting(key=top, value=merged[top]))
        else:
            row.value = json.loads(json.dumps(merged[top]))
            session.add(row)
        label = SETTING_LABELS.get(key, key)
        if key == "agent_paused":
            summary = "Paused the agent" if after_flat[key] else "Resumed the agent"
        else:
            summary = (
                f"Changed {label} from {_describe(key, before_flat.get(key))} "
                f"to {_describe(key, after_flat[key])}"
            )
        audit(
            session,
            now,
            actor,
            "settings_changed",
            "settings",
            key,
            summary,
            {key: before_flat.get(key)},
            {key: after_flat[key]},
        )
    session.commit()
    return merged


# --- proposals --------------------------------------------------------------------------


def _target_label(target: str, cfg: Config) -> str:
    if cfg.is_fixture:
        return "fixture mode, nothing was written to Dynatrace"
    return {
        "dry-run": "dry-run target, nothing was written",
        "openpipeline": "OpenPipeline",
        "otelcol": "OpenTelemetry Collector config",
    }[target]


def create_proposal(
    session: Session, definition: dict[str, Any], now: datetime, run_id: str | None = None
) -> Proposal:
    if session.get(Proposal, definition["id"]) is not None:
        raise ConflictError(f"proposal {definition['id']} already exists")
    p = Proposal(
        **{k: v for k, v in definition.items() if k in Proposal.model_fields},
        status="awaiting_approval",
        created_at=now,
        run_id=run_id,
    )
    session.add(p)
    audit(
        session,
        now,
        "agent",
        "proposal_created",
        "proposal",
        p.id,
        f"Proposed “{p.title}”",
        None,
        {"status": "awaiting_approval"},
    )
    session.commit()
    return p


def _get(session: Session, pid: str) -> Proposal:
    p = session.get(Proposal, pid)
    if p is None:
        raise NotFoundError(f"proposal {pid} not found")
    return p


def _window(cfg: Config, settings: dict[str, Any]) -> timedelta:
    if cfg.is_fixture:
        return timedelta(seconds=float(settings.get("fixture_verify_seconds", 8)))
    return timedelta(minutes=int(settings.get("verify_window_min", cfg.verify_window_min)))


def approve(
    session: Session,
    cfg: Config,
    pid: str,
    actor: str,
    now: datetime,
    window: timedelta | None = None,
) -> Proposal:
    p = _get(session, pid)
    if p.status != "awaiting_approval":
        raise ConflictError(f"cannot approve a proposal that is {p.status}")
    world = engine.load_world(session, now)
    blocking = engine.blocking_guardrails(world, p)
    if blocking:
        names = ", ".join(g.name for g in blocking)
        raise ConflictError(f"blocked by guardrail: {names}")
    settings = world.settings
    window = window or _window(cfg, settings)

    p.status, p.decided_by, p.decided_at = "approved", actor, now
    audit(
        session,
        now,
        actor,
        "approved",
        "proposal",
        p.id,
        f"Approved “{p.title}”",
        {"status": "awaiting_approval"},
        {"status": "approved"},
    )

    # Store the previous state first, so a revert can restore it.
    p.previous_state = {"target": cfg.apply_target, "rule_present": False}
    p.applied_at, p.applied_target, p.status = now, cfg.apply_target, "applied"
    audit(
        session,
        now,
        "agent",
        "applied",
        "proposal",
        p.id,
        f"Applied the rule ({_target_label(cfg.apply_target, cfg)})",
        {"status": "approved"},
        {"status": "applied", "target": cfg.apply_target},
    )

    p.status, p.verify_due_at = "verifying", now + window
    minutes = int(settings.get("verify_window_min", cfg.verify_window_min))
    compressed = cfg.is_fixture and window != timedelta(minutes=minutes)
    detail = f" (fixture mode: {window.total_seconds():.0f} s)" if compressed else ""
    audit(
        session,
        now,
        "agent",
        "verification_started",
        "proposal",
        p.id,
        f"Started a {minutes} min verification window{detail}",
        {"status": "applied"},
        {"status": "verifying"},
    )
    session.add(p)
    session.commit()
    return p


def reject(session: Session, pid: str, actor: str, now: datetime, reason: str | None) -> Proposal:
    p = _get(session, pid)
    if p.status != "awaiting_approval":
        raise ConflictError(f"cannot reject a proposal that is {p.status}")
    p.status, p.decided_by, p.decided_at = "rejected", actor, now
    p.reject_reason = (reason or "").strip() or None
    summary = f"Rejected “{p.title}”" + (f": {p.reject_reason}" if p.reject_reason else "")
    audit(
        session,
        now,
        actor,
        "rejected",
        "proposal",
        p.id,
        summary,
        {"status": "awaiting_approval"},
        {"status": "rejected"},
    )
    session.add(p)
    session.commit()
    return p


def revert(session: Session, pid: str, actor: str, now: datetime, reason: str | None) -> Proposal:
    p = _get(session, pid)
    if p.status not in engine.ACTIVE:
        raise ConflictError(f"cannot revert a proposal that is {p.status}")
    before = p.status
    p.status, p.reverted_at = "reverted", now
    p.revert_reason = (reason or "").strip() or "Reverted by hand"
    audit(
        session,
        now,
        actor,
        "reverted",
        "proposal",
        p.id,
        f"Reverted “{p.title}”. Previous state restored",
        {"status": before},
        {"status": "reverted", "previous_state": p.previous_state},
    )
    session.add(p)
    session.commit()
    return p


def _checked_refs(world: engine.World, p: Proposal) -> list[str]:
    sig = world.signals[p.signal_id]
    refs = [r for r in engine.affecting_refs(world, sig) if r.type in engine.STRONG_REF_TYPES]
    refs += engine.related_refs(world, sig)
    names: list[str] = []
    for r in refs:
        label = f"{r.type.capitalize()} “{r.name}”"
        if label not in names:
            names.append(label)
    return names


def verify(
    session: Session, pid: str, now: datetime, fail: dict[str, str] | None = None
) -> Proposal:
    """Fixture verification. Live mode re-runs volume, reference and Davis checks (phase 04)."""
    p = _get(session, pid)
    if p.status != "verifying":
        raise ConflictError(f"cannot verify a proposal that is {p.status}")
    world = engine.load_world(session, now)
    s = engine.savings(world, p)
    checked = _checked_refs(world, p)
    if p.action == "redact":
        volume = {
            "passed": True,
            "detail": "Matching records still arrive, with the address masked",
        }
    else:
        observed = s.gib_day * 0.98
        volume = {
            "passed": True,
            "detail": f"−{fmt.gib(observed)} a day (predicted −{fmt.gib(s.gib_day)})",
        }
    checks = [
        {"name": "volume", "label": "Volume changed as predicted", **volume},
        {
            "name": "references",
            "label": "Queries, dashboards and alerts still return data",
            "passed": True,
            "detail": f"{len(checked)} of {len(checked)} still return data"
            if checked
            else "Nothing references this data",
        },
        {
            "name": "problems",
            "label": "No new Davis problems",
            "passed": True,
            "detail": "0 new problems",
        },
    ]
    if fail:
        for c in checks:
            if c["name"] == fail.get("check", "references"):
                c["passed"], c["detail"] = False, fail["detail"]
    passed = all(c["passed"] for c in checks)
    result: dict[str, Any] = {
        "passed": passed,
        "checks": checks,
        "checked": checked,
        "at": now.isoformat(),
    }
    if passed:
        p.status, p.verified_at = "verified", now
        refs_text = checks[1]["detail"]
        if checked:
            refs_text = f"{len(checked)} of {len(checked)} references still return data"
        audit(
            session,
            now,
            "agent",
            "verified",
            "proposal",
            p.id,
            f"Verified “{p.title}”. Volume {checks[0]['detail'][0].lower()}"
            f"{checks[0]['detail'][1:]}; {refs_text[0].lower()}{refs_text[1:]}; "
            "no new Davis problems",
            {"status": "verifying"},
            {"status": "verified"},
        )
    else:
        failed = next(c for c in checks if not c["passed"])
        assert p.applied_at is not None
        minutes = max(1, round((now - p.applied_at).total_seconds() / 60))
        result["incident"] = (
            f"“{p.title}” was reverted {minutes} min after it was applied. {failed['detail']} "
            "Sift restored the previous state; nothing else changed."
        )
        audit(
            session,
            now,
            "agent",
            "verification_failed",
            "proposal",
            p.id,
            f"Verification failed for “{p.title}”: {failed['detail']}",
            {"status": "verifying"},
            {"status": "verifying", "check": failed["name"]},
        )
        p.status, p.reverted_at = "reverted", now
        p.revert_reason = f"Verification failed: {failed['detail']}"
        audit(
            session,
            now,
            "agent",
            "reverted",
            "proposal",
            p.id,
            f"Reverted “{p.title}” automatically. Previous state restored",
            {"status": "verifying"},
            {"status": "reverted", "previous_state": p.previous_state},
        )
    p.verify_result = result
    session.add(p)
    session.commit()
    return p


def run_due_verifications(session: Session, now: datetime) -> list[str]:
    """Called by the background loop. Returns the ids it finished."""
    due = session.exec(
        select(Proposal).where(Proposal.status == "verifying", Proposal.verify_due_at <= now)
    ).all()
    done: list[str] = []
    for p in due:
        settings = get_settings(session)
        fail = None
        if settings.get("fixture_fail_next_verification"):
            world = engine.load_world(session, now)
            checked = _checked_refs(world, p)
            fail = (
                {
                    "check": "references",
                    "detail": f"{checked[0]} returned no data after the rule was applied.",
                }
                if checked
                else {"check": "volume", "detail": "Volume did not drop: the matcher hit nothing."}
            )
            row = session.get(Setting, "fixture_fail_next_verification")
            assert row is not None
            row.value = False
            session.add(row)
        verify(session, p.id, now, fail)
        done.append(p.id)
    return done


# --- guardrails -------------------------------------------------------------------------


def add_guardrail(session: Session, data: dict[str, Any], actor: str, now: datetime) -> Guardrail:
    gid = data.get("id") or _slug(data["name"])
    if session.get(Guardrail, gid) is not None:
        raise ConflictError(f"guardrail {gid} already exists")
    g = Guardrail(
        id=gid,
        builtin=False,
        name=data["name"].strip(),
        reason=data["reason"].strip(),
        service=data.get("service") or None,
        kind=data.get("kind") or None,
        key_pattern=data.get("key_pattern") or None,
        created_by=actor,
        created_at=now,
    )
    if not (g.service or g.kind or g.key_pattern):
        raise ConflictError("a guardrail needs a service, a kind or a key pattern")
    session.add(g)
    audit(
        session,
        now,
        actor,
        "guardrail_added",
        "guardrail",
        g.id,
        f"Protected {g.name}: {g.reason}",
        None,
        {"name": g.name},
    )
    session.commit()
    return g


def delete_guardrail(session: Session, gid: str, actor: str, now: datetime) -> None:
    g = session.get(Guardrail, gid)
    if g is None:
        raise NotFoundError(f"guardrail {gid} not found")
    if g.builtin:
        raise ConflictError("built-in guardrails cannot be removed")
    session.delete(g)
    audit(
        session,
        now,
        actor,
        "guardrail_removed",
        "guardrail",
        gid,
        f"Removed the guardrail “{g.name}”",
        {"name": g.name},
        None,
    )
    session.commit()


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return f"g-{slug[:40]}" if slug else "g-custom"
