"""`SIFT_APPLY_TARGET=dry-run` (the default): never writes. The audit log says what would
have been added where, from the same rule the real targets write."""

from __future__ import annotations

from typing import Any

from server.agent import rules
from server.apply import Applied, Reverted, check_approved
from server.config import Config
from server.models import Proposal


class DryRunApplier:
    target = "dry-run"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def snapshot(self, p: Proposal) -> dict[str, Any]:
        return {"target": self.target, "rule_present": False}

    def write(self, p: Proposal, snapshot: dict[str, Any]) -> Applied:
        check_approved(p)
        op = (p.rule.get("openpipeline") or {}).get("settings")
        would: dict[str, Any] = {}
        if op:
            would["openpipeline"] = {
                "schemaId": op["schemaId"],
                "processor_ids": rules.processor_ids(op, p.id),
            }
        otel = (p.rule.get("otel") or {}).get("code")
        if otel:
            would["otelcol"] = {"components": _component_ids(otel)}
        ids = [*would.get("openpipeline", {}).get("processor_ids", [])]
        what = ", ".join(ids) if ids else "the rule"
        return Applied(
            target=self.target,
            summary=f"Dry run: nothing was written (would add {what})",
            ref={"dry_run": True, "would_write": would},
            changed=False,
        )

    def revert(self, p: Proposal) -> Reverted:
        return Reverted("Dry run: nothing was written, nothing to restore", "nothing")


def _component_ids(code: str) -> list[str]:
    import yaml

    doc = yaml.safe_load(code.partition("\n#")[0]) or {}
    return [cid for sec in ("processors", "connectors") for cid in (doc.get(sec) or {})]
