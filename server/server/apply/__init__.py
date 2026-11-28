"""Apply targets: where an approved rule goes (`SIFT_APPLY_TARGET`).

Every target works in two steps so the previous state is stored before anything is written:

1. `snapshot(p)` reads the current configuration (OpenPipeline pipeline object or the
   Sift-managed Collector config) and returns it without this proposal's own parts;
   `services.approve` saves it on the proposal and commits.
2. `write(p, snapshot)` adds this proposal's processors (ids carry the proposal id, so a
   second write is a no-op) and returns what it wrote.

`revert(p)` restores the snapshot when the configuration is still exactly what Sift wrote,
and otherwise removes only this proposal's processors, so other people's later edits survive.

Approval is the only way in: `write` refuses a proposal that is not `approved` by a person
or whose rule changed since it was approved (`rule_version`)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from server.config import Config
from server.models import Proposal


class ApplyError(RuntimeError):
    """The rule could not be applied or reverted; nothing was left half-written."""


@dataclass
class Applied:
    target: str
    summary: str  # one sentence for the audit log
    ref: dict[str, Any]  # what was written: ids, file / object, digest of the result
    changed: bool = True  # False: the configuration already had exactly this rule
    log: list[str] = field(default_factory=list)


@dataclass
class Reverted:
    summary: str
    restored: str  # snapshot | removed_own | nothing
    log: list[str] = field(default_factory=list)


class Applier(Protocol):
    target: str

    def snapshot(self, p: Proposal) -> dict[str, Any]: ...

    def write(self, p: Proposal, snapshot: dict[str, Any]) -> Applied: ...

    def revert(self, p: Proposal) -> Reverted: ...


def digest(obj: Any) -> str:
    blob = obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def rule_version(rule: dict[str, Any]) -> str:
    """Content hash of what would be written (both targets). Shown in the approve dialog and
    stored with the approval, so an approval covers exactly one version of the rule."""
    op = rule.get("openpipeline") or {}
    otel = rule.get("otel") or {}
    return "r-" + digest({"op": op.get("settings") or op.get("code"), "otel": otel.get("code")})[:8]


def check_approved(p: Proposal) -> None:
    if p.status != "approved" or not p.decided_by:
        raise ApplyError(f"{p.id} is {p.status}: only an approved proposal can be applied")
    if p.rule_version != rule_version(p.rule):
        raise ApplyError(f"{p.id}: the rule changed after it was approved; approve it again")


def fixture_state_dir(cfg: Config) -> Path:
    """Fixture mode keeps the OpenPipeline settings it "writes" next to the database."""
    return cfg.db_path.parent / "fixture-settings"


def applier_for(cfg: Config, target: str) -> Applier:
    if target == "dry-run":
        from server.apply.dryrun import DryRunApplier

        return DryRunApplier(cfg)
    if target == "otelcol":
        from server.apply.otelcol import OtelcolApplier

        return OtelcolApplier.from_config(cfg)
    if target == "openpipeline":
        from server.apply.openpipeline import OpenPipelineApplier

        return OpenPipelineApplier.from_config(cfg)
    raise ApplyError(f"unknown apply target {target!r}")


def reset_fixture_state(cfg: Config) -> None:
    """Demo reset: forget what fixture mode wrote (Sift-managed files only)."""
    if not cfg.is_fixture:
        return
    state = fixture_state_dir(cfg)
    if state.is_dir():
        for f in state.glob("*.json"):
            f.unlink()
    managed = cfg.otelcol_config
    data_dir = cfg.db_path.parent.resolve()
    if managed.exists() and data_dir in managed.resolve().parents:
        managed.unlink()
