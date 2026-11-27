"""Fixture files (fixtures/*.json) and demo seeding.

Seeding replays fixtures/scenario.json through the same services the API uses."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, delete

from server import services
from server.config import Config
from server.connectors import fixture_connectors
from server.engines import pipeline
from server.engines.usage import RecordedResolver
from server.models import (
    AuditEvent,
    Guardrail,
    Proposal,
    Reference,
    Run,
    RunEvent,
    Setting,
    Signal,
)

_OFFSET = re.compile(r"^-(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$")


def offset(text: str, now: datetime) -> datetime:
    """'-2d4h30m' -> now minus 2 days, 4 hours, 30 minutes."""
    m = _OFFSET.match(text)
    if not m or not any(m.groups()):
        raise ValueError(f"bad offset {text!r}")
    d, h, mins = (int(g or 0) for g in m.groups())
    return now - timedelta(days=d, hours=h, minutes=mins)


@lru_cache(maxsize=8)
def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def load(cfg: Config, name: str) -> dict[str, Any]:
    return _read(cfg.fixtures_dir / f"{name}.json")


def proposal_definitions(cfg: Config) -> dict[str, dict[str, Any]]:
    data = load(cfg, "proposals")
    notes = data["notes"]
    out: dict[str, dict[str, Any]] = {}
    for d in data["proposals"]:
        d = json.loads(json.dumps(d))  # deep copy, the cache must not be mutated
        d["rule"]["openpipeline"]["note"] = notes["openpipeline"]
        d["rule"]["otel"]["note"] = notes["otel"]
        out[d["id"]] = d
    return out


def environment(cfg: Config) -> dict[str, Any]:
    """Environment id and name from the recording (fixtures/dynatrace/environment.json)."""
    return json.loads((cfg.fixtures_dir / "dynatrace" / "environment.json").read_text())


def unattributed_gib_day(session: Session) -> dict[str, float]:
    """GiB/day of billed data types without signals (events), stored by discovery."""
    row = session.get(Setting, "unattributed_gib_day")
    return {k: float(v) for k, v in (row.value if row else {}).items()}


def discover(cfg: Config, settings: dict[str, Any], now: datetime) -> pipeline.Snapshot:
    """Run the discovery pipeline on the recorded Dynatrace responses."""
    answers = load(cfg, "agent/usage_resolutions")["answers"]
    conn = fixture_connectors(cfg.fixtures_dir / "dynatrace", now)
    return pipeline.run(conn, settings, now, RecordedResolver(answers))


def _store_snapshot(session: Session, snap: pipeline.Snapshot, findings: dict[str, Any]) -> None:
    for d in snap.signals:
        f = findings.get(d.id) or {}
        session.add(
            Signal(
                id=d.id,
                kind=d.kind,
                data_type=d.data_type,
                service=d.service,
                key=d.key,
                parent_id=d.parent_id,
                source=d.source,
                level=d.level,
                gib_day=d.gib_day,
                avg_record_bytes=d.avg_record_bytes,
                growth_wow=round(d.growth_wow, 6),
                first_seen=d.first_seen.replace(microsecond=0),
                finding_class=f.get("class"),
                finding_note=f.get("note"),
                samples=d.samples,
            )
        )
    for r in snap.references:
        session.add(
            Reference(
                signal_id=r.signal_id,
                type=r.type,
                name=r.name,
                owner=r.owner,
                count_30d=r.count_30d,
                last_used=r.last_used.replace(microsecond=0) if r.last_used else None,
                fields=r.fields,
                dql=r.dql,
                note=r.note,
            )
        )
    session.add(Setting(key="unattributed_gib_day", value=snap.unattributed_gib_day))
    session.add(
        Setting(
            key="discovery",
            value={
                "queries": len(snap.queries),
                "scanned_bytes": snap.scanned_bytes,
                "calibration": snap.calibration,
                "unresolved": [
                    {
                        "type": u.site.type,
                        "name": u.site.name,
                        "part": u.site.part,
                        "reason": u.reason,
                    }
                    for u in snap.unresolved
                ],
                "mcp_tools": snap.mcp_tools,
                "warnings": snap.warnings,
            },
        )
    )


TABLES: list[type[SQLModel]] = [
    AuditEvent,
    RunEvent,
    Run,
    Proposal,
    Reference,
    Signal,
    Guardrail,
    Setting,
]


def seed(engine: Engine, cfg: Config, now: datetime, *, history: bool = True) -> None:
    """Wipe the database and rebuild the demo world as of `now`.

    history=False stops after discovery: no proposals, runs or custom guardrails. That
    "clean" state is where the recorded analyze run (fixtures/agent/analyze.json) was made."""
    from server import runs  # local import: runs depends on this module

    with Session(engine, expire_on_commit=False) as session:
        for table in TABLES:
            session.exec(delete(table))  # type: ignore[call-overload]
        session.commit()

        settings = load(cfg, "settings")
        initial = json.loads(json.dumps(settings["settings"]))
        initial["verify_window_min"] = cfg.verify_window_min
        initial["fixture_verify_seconds"] = cfg.fixture_verify_seconds
        snap = discover(cfg, initial, now)
        _store_snapshot(session, snap, load(cfg, "agent/findings")["findings"])
        for key, value in initial.items():
            session.add(Setting(key=key, value=value))
        for g in settings["guardrails"]:
            session.add(Guardrail(**g, created_at=offset("-90d", now)))
        session.commit()

        user = initial["user_name"]
        defs = proposal_definitions(cfg)
        for step in load(cfg, "scenario")["steps"] if history else []:
            at = offset(step["at"], now)
            do = step["do"]
            if do == "create":
                services.create_proposal(session, defs[step["proposal"]], at)
            elif do == "approve":
                services.approve(
                    session,
                    cfg,
                    step["proposal"],
                    user,
                    at,
                    window=timedelta(minutes=cfg.verify_window_min),
                )
            elif do == "verify":
                services.verify(session, step["proposal"], at, step.get("fail"))
            elif do == "reject":
                services.reject(session, step["proposal"], user, at, step.get("reason"))
            elif do == "guardrail_add":
                services.add_guardrail(session, step["guardrail"], user, at)
            elif do == "settings":
                services.update_settings(session, step["patch"], user, at)
            elif do == "run":
                runs.replay_sync(session, cfg, step["type"], step.get("trigger", "user"), at)
            else:
                raise ValueError(f"unknown scenario step {do!r}")
        session.commit()
