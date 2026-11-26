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
    return load(cfg, "signals")["environment"]


def unattributed_gib_day(cfg: Config) -> dict[str, float]:
    return {k: v / 7 for k, v in environment(cfg)["unattributed_gib_week"].items()}


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


def seed(engine: Engine, cfg: Config, now: datetime) -> None:
    """Wipe the database and rebuild the demo world as of `now`."""
    from server import runs  # local import: runs depends on this module

    with Session(engine, expire_on_commit=False) as session:
        for table in TABLES:
            session.exec(delete(table))  # type: ignore[call-overload]
        session.commit()

        world = load(cfg, "signals")
        for s in world["signals"]:
            session.add(
                Signal(
                    id=s["id"],
                    kind=s["kind"],
                    data_type=s["data_type"],
                    service=s["service"],
                    key=s["key"],
                    parent_id=s.get("parent_id"),
                    source=s.get("source"),
                    level=s.get("level"),
                    gib_day=s["gib_week"] / 7,
                    avg_record_bytes=s["avg_record_bytes"],
                    growth_wow=s.get("growth_wow", 0.0),
                    first_seen=offset(s["first_seen"], now),
                    finding_class=s.get("finding_class"),
                    finding_note=s.get("finding_note"),
                    samples=s.get("samples", []),
                )
            )
        for r in world["references"]:
            session.add(
                Reference(
                    signal_id=r["signal_id"],
                    type=r["type"],
                    name=r["name"],
                    owner=r["owner"],
                    count_30d=r.get("count_30d", 0),
                    last_used=offset(r["last_used"], now) if r.get("last_used") else None,
                    fields=r.get("fields", []),
                    dql=r.get("dql"),
                    note=r.get("note"),
                )
            )
        settings = load(cfg, "settings")
        initial = json.loads(json.dumps(settings["settings"]))
        initial["verify_window_min"] = cfg.verify_window_min
        initial["fixture_verify_seconds"] = cfg.fixture_verify_seconds
        for key, value in initial.items():
            session.add(Setting(key=key, value=value))
        for g in settings["guardrails"]:
            session.add(Guardrail(**g, created_at=offset("-90d", now)))
        session.commit()

        user = initial["user_name"]
        defs = proposal_definitions(cfg)
        for step in load(cfg, "scenario")["steps"]:
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
