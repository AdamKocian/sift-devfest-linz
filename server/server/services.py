"""State changes: proposals, verification, settings and guardrails. Every change writes an
AuditEvent with the actor, before and after."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from server import apply, engine, verify
from server.agent import rules
from server.config import Config
from server.connectors.base import ConnectorError
from server.models import AuditEvent, Guardrail, Proposal, Setting


class ConflictError(Exception):
    """The requested change is not allowed in the current state (HTTP 409)."""


class ApplyFailedError(ConflictError):
    """The apply target refused the change; nothing was left half-written (HTTP 409)."""


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


def _snapshot_info(snap: dict[str, Any] | None) -> dict[str, Any] | None:
    """The snapshot for the audit log, without the stored file or pipeline value."""
    if not snap:
        return snap
    return {k: v for k, v in snap.items() if k not in ("content", "value")}


def create_proposal(
    session: Session, definition: dict[str, Any], now: datetime, run_id: str | None = None
) -> Proposal:
    if session.get(Proposal, definition["id"]) is not None:
        raise ConflictError(f"proposal {definition['id']} already exists")
    # Processor and component ids carry the proposal id (idempotent apply, exact revert).
    definition = {
        **definition,
        "rule": rules.finalize(definition.get("rule") or {}, definition["id"]),
    }
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
    rule_version: str | None = None,
) -> Proposal:
    """The only path to apply: a person approves one version of the rule; then the previous
    state is read and stored, and only then is the rule written (server.apply)."""
    p = _get(session, pid)
    if p.status != "awaiting_approval":
        raise ConflictError(f"cannot approve a proposal that is {p.status}")
    world = engine.load_world(session, now)
    blocking = engine.blocking_guardrails(world, p)
    if blocking:
        names = ", ".join(g.name for g in blocking)
        raise ConflictError(f"blocked by guardrail: {names}")
    version = apply.rule_version(p.rule)
    if rule_version and rule_version != version:
        raise ConflictError(
            f"the rule changed since you opened it (now {version}); review it and approve again"
        )
    settings = world.settings
    window = window or _window(cfg, settings)
    target = cfg.apply_target

    p.status, p.decided_by, p.decided_at, p.rule_version = "approved", actor, now, version
    audit(
        session,
        now,
        actor,
        "approved",
        "proposal",
        p.id,
        f"Approved “{p.title}” (rule {version})",
        {"status": "awaiting_approval"},
        {"status": "approved", "rule_version": version, "target": target},
    )
    try:
        applier = apply.applier_for(cfg, target)
        # 1. read the current configuration and store it, so a revert can restore it
        p.previous_state, p.applied_target = applier.snapshot(p), target
        session.add(p)
        session.commit()
        # 2. write the rule
        result = applier.write(p, p.previous_state)
    except (apply.ApplyError, ConnectorError) as e:
        _apply_failed(session, p, now, str(e))
        raise ApplyFailedError(f"could not apply: {e}") from e

    p.applied_at, p.applied_ref, p.status = now, result.ref, "applied"
    where = (
        " (fixture mode: recorded pipeline settings, nothing reaches Dynatrace)"
        if cfg.is_fixture and target == "openpipeline"
        else ""
    )
    audit(
        session,
        now,
        "agent",
        "applied",
        "proposal",
        p.id,
        f"Applied the rule: {result.summary}{where}",
        {"status": "approved", "previous_state": _snapshot_info(p.previous_state)},
        {"status": "applied", "target": target, "rule_version": version, "ref": result.ref},
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


def _apply_failed(session: Session, p: Proposal, now: datetime, reason: str) -> None:
    audit(
        session,
        now,
        "agent",
        "apply_failed",
        "proposal",
        p.id,
        f"Could not apply “{p.title}”: {reason}. Nothing was changed; it waits for approval again",
        {"status": "approved"},
        {"status": "awaiting_approval"},
    )
    p.status, p.decided_by, p.decided_at = "awaiting_approval", None, None
    p.rule_version = p.previous_state = p.applied_target = None
    session.add(p)
    session.commit()


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


def _undo(session: Session, cfg: Config, p: Proposal, now: datetime) -> apply.Reverted:
    try:
        return apply.applier_for(cfg, p.applied_target or "dry-run").revert(p)
    except (apply.ApplyError, ConnectorError) as e:
        audit(
            session,
            now,
            "agent",
            "revert_failed",
            "proposal",
            p.id,
            f"Could not revert “{p.title}”: {e}. The rule is still in place",
            {"status": p.status},
            {"status": p.status},
        )
        session.commit()
        raise ApplyFailedError(f"could not revert: {e}") from e


def revert(
    session: Session, cfg: Config, pid: str, actor: str, now: datetime, reason: str | None
) -> Proposal:
    p = _get(session, pid)
    if p.status not in engine.ACTIVE:
        raise ConflictError(f"cannot revert a proposal that is {p.status}")
    before = p.status
    r = _undo(session, cfg, p, now)
    p.status, p.reverted_at = "reverted", now
    p.revert_reason = (reason or "").strip() or "Reverted by hand"
    audit(
        session,
        now,
        actor,
        "reverted",
        "proposal",
        p.id,
        f"Reverted “{p.title}”. {r.summary}",
        {"status": before},
        {
            "status": "reverted",
            "restored": r.restored,
            "previous_state": _snapshot_info(p.previous_state),
        },
    )
    session.add(p)
    session.commit()
    return p


def _evidence(
    cfg: Config, p: Proposal, w: engine.World, window: int
) -> tuple[verify.Plan, verify.Evidence, verify.RecordedEvidence | None]:
    if cfg.is_fixture:
        rec = verify.recording(cfg.fixtures_dir, p.id)
        if rec is not None:
            pl = verify.plan(w, p, int(rec.rec["window_min"]))
            return pl, rec, rec
        pl = verify.plan(w, p, window)
        return pl, verify.DerivedEvidence(w, p, pl), None
    from server.connectors import DynatraceEnv, live_connectors

    conn = live_connectors(DynatraceEnv(cfg.platform_url, cfg.dt_platform_token, cfg.dt_mcp_url))
    return verify.plan(w, p, window), verify.LiveEvidence(conn.grail, conn.mcp), None


def verify_proposal(
    session: Session,
    cfg: Config,
    pid: str,
    now: datetime,
    *,
    fail_next: bool = False,
    evidence: verify.Evidence | None = None,
    writer: Callable[[dict[str, Any]], str] | None = None,
) -> Proposal:
    """Run the three checks; on failure revert from the snapshot and store an incident note."""
    p = _get(session, pid)
    if p.status != "verifying":
        raise ConflictError(f"cannot verify a proposal that is {p.status}")
    w = engine.load_world(session, now)
    window = int(w.settings.get("verify_window_min", cfg.verify_window_min))
    pl, ev, rec = _evidence(cfg, p, w, window)
    if evidence is not None:
        ev, rec = evidence, None
    if fail_next:
        ev = verify.FailingEvidence(ev, pl)
    try:
        checks = verify.run_checks(w, p, pl, ev)
    except ConnectorError as e:
        if cfg.is_fixture:
            raise  # a recording that does not match the code is a bug, not a failed rule
        checks = [
            {
                "name": "volume",
                "label": "Volume changed as predicted",
                "passed": False,
                "detail": f"Verification could not run: {e}",
            },
        ]
    passed = all(c["passed"] for c in checks)
    result: dict[str, Any] = {
        "passed": passed,
        "checks": checks,
        "checked": [r.label for r in pl.refs],
        "at": now.isoformat(),
        "source": ev.source,
        "window_min": pl.window_min,
    }
    if rec is not None:
        result["recording"] = f"fixtures/verify/{rec.path.name}"
    if passed:
        p.status, p.verified_at = "verified", now
        vol = checks[0]["detail"]
        refs = checks[1]["detail"]
        if pl.refs and checks[1]["passed"]:
            refs = f"{refs.split(' still')[0]} references still return data"
        audit(
            session,
            now,
            "agent",
            "verified",
            "proposal",
            p.id,
            f"Verified “{p.title}”. {vol[0].upper()}{vol[1:]}; {refs[0].lower()}{refs[1:]}; "
            "no new Davis problems",
            {"status": "verifying"},
            {"status": "verified", "source": ev.source},
        )
        p.verify_result = result
        session.add(p)
        session.commit()
        return p

    failed = next(c for c in checks if not c["passed"])
    assert p.applied_at is not None
    minutes = max(1, round((now - p.applied_at).total_seconds() / 60))
    if writer is None and cfg.agent == "gemini" and not cfg.is_fixture:
        from server.agent.incident import gemini_writer

        writer = gemini_writer(cfg)
    recorded = rec.note if rec is not None and not fail_next else None
    inc = verify.incident_note(p, checks, minutes, recorded=recorded, writer=writer)
    result["incident"], result["incident_by"] = inc.text, inc.by
    audit(
        session,
        now,
        "agent",
        "verification_failed",
        "proposal",
        p.id,
        f"Verification failed for “{p.title}”: {failed['detail']}",
        {"status": "verifying"},
        {"status": "verifying", "check": failed["name"], "source": ev.source},
    )
    p.verify_result = result
    try:
        r = _undo(session, cfg, p, now)
    except ApplyFailedError:
        # Try again in a minute; the rule stays marked as verifying, the log says why.
        p.verify_due_at = now + timedelta(minutes=1)
        session.add(p)
        session.commit()
        return p
    p.status, p.reverted_at = "reverted", now
    p.revert_reason = f"Verification failed: {failed['detail']}"
    audit(
        session,
        now,
        "agent",
        "reverted",
        "proposal",
        p.id,
        f"Reverted “{p.title}” automatically. {r.summary}",
        {"status": "verifying"},
        {
            "status": "reverted",
            "restored": r.restored,
            "previous_state": _snapshot_info(p.previous_state),
        },
    )
    session.add(p)
    session.commit()
    return p


def run_due_verifications(session: Session, cfg: Config, now: datetime) -> list[str]:
    """Called by the background loop. Returns the ids it finished."""
    due = session.exec(
        select(Proposal).where(Proposal.status == "verifying", Proposal.verify_due_at <= now)
    ).all()
    done: list[str] = []
    for p in due:
        fail = False
        if cfg.is_fixture and get_settings(session).get("fixture_fail_next_verification"):
            fail = True
            row = session.get(Setting, "fixture_fail_next_verification")
            assert row is not None
            row.value = False
            session.add(row)
        verify_proposal(session, cfg, p.id, now, fail_next=fail)
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
