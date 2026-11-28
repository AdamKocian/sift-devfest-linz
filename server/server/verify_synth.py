"""Synthetic verification recordings: fixtures/verify/<proposal id>.json (`make fixtures-synth`).

For every proposal of the demo (the four in fixtures/proposals.json and the ones the recorded
analysis run creates) this writes the answers the three verification checks would get at a
30 min window: the Grail volume record, one Grail answer per re-run reference, and the
Dynatrace MCP `execute_dql` result for new Davis problems. The query texts are the ones
`server.verify.plan` builds, so a code change that alters a query makes replay fail loudly.

One recording is a failure on purpose: removing db.statement from inventory spans
(p-inventory-dbstatement) empties the "Inventory DB latency" dashboard, the demo's
auto-revert. Its incident note stands in for the one the agent writes in live mode.
Replace these files with real recordings once a tenant exists."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session

from server import engine, fixtures, verify
from server.config import load_config
from server.connectors.store import format_ts
from server.db import make_engine

WINDOW_MIN = 30
FAILING = "p-inventory-dbstatement"
FAILING_REF = "dashboard:Inventory DB latency"
INCIDENT = (
    "Removing db.statement from “SELECT inventory.stock” spans emptied the dashboard "
    "“Inventory DB latency”: its “Slowest statements” tile groups by that attribute and "
    "returned no rows after the rule. Span volume changed as predicted and no Davis problem "
    "opened, so the rule did what it said, but the attribute has a reader the usage scan "
    "did not attribute to it. Next step: protect db.statement on inventory spans with a "
    "guardrail, or ask the dashboard owner whether the tile is still needed."
)


def _unit(key: str) -> float:
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def build(out: Path) -> list[Path]:
    cfg = load_config()
    tmp = Path(tempfile.mkdtemp())
    cfg = replace(cfg, db_path=tmp / "sift.db", replay_speed=0, apply_target="dry-run")
    now = datetime.now(UTC).replace(microsecond=0)
    original = verify.recording
    verify.recording = lambda *_a, **_k: None  # type: ignore[assignment]  # seed without them
    try:
        db = make_engine(cfg.db_path)
        fixtures.seed(db, cfg, now)
    finally:
        verify.recording = original  # type: ignore[assignment]
    written: list[Path] = []
    with Session(db) as s:
        w = engine.load_world(s, now)
        for p in w.proposals:
            pl = verify.plan(w, p, WINDOW_MIN)
            derived = verify.DerivedEvidence(w, p, pl).volume(pl.volume)
            before = float(derived["before"])
            obs = 0.965 + 0.03 * _unit(p.id)  # 96.5-99.5 % of the predicted drop
            vol = {"before": round(before), "after": round(before * (1 - pl.predicted * obs))}
            if pl.kind == "redact":
                vol = {"before": round(before), "after": round(before * 0.99), "unmasked_after": 0}
            refs = {}
            for r in pl.refs:
                if r.query is None:
                    continue
                n = 0 if (p.id == FAILING and r.key == FAILING_REF) else 1 + int(40 * _unit(r.key))
                refs[r.key] = {"query": r.query.dql, "records": n}
            rec = {
                "_about": (
                    f"Synthetic recording of the verification of {p.id} (`make fixtures-synth`, "
                    "server/server/verify_synth.py): Grail answers for the volume and reference "
                    "queries and the Dynatrace MCP execute_dql answer for new Davis problems. "
                    "The MCP result format is not documented; this uses a JSON text block. "
                    "Replace with real recordings once a tenant exists."
                ),
                "proposal": p.id,
                "recorded_at": format_ts(now),
                "window_min": WINDOW_MIN,
                "volume": {"query": pl.volume.dql, "records": [vol]},
                "references": refs,
                "problems": {
                    "tool": "execute_dql",
                    "query": pl.problems.dql,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps({"records": []})}],
                        "isError": False,
                    },
                },
            }
            if p.id == FAILING:
                if FAILING_REF not in refs:
                    raise SystemExit(f"{FAILING}: expected a re-run of {FAILING_REF}")
                rec["incident_note"] = INCIDENT
            path = out / f"{p.id}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rec, indent=1, ensure_ascii=False) + "\n")
            written.append(path)
    return written


if __name__ == "__main__":
    cfg = load_config()
    paths = build(cfg.fixtures_dir / "verify")
    print(f"{len(paths)} verification recordings in fixtures/verify/")
