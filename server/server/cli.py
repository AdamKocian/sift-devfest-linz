"""Command line.

uv run python -m server.cli reset                 rebuild the demo database
uv run python -m server.cli discover [--live] [--top 10] [--sampling 1]
                                                  run discovery and print the top signals
uv run python -m server.cli record [--sampling 1] record live responses into fixtures/
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from server.config import REPO_ROOT, load_config
from server.connectors import DynatraceEnv, fixture_connectors, live_connectors
from server.db import make_engine, utcnow
from server.engines import pipeline
from server.engines.cost import CostModel
from server.engines.discover import GIB
from server.engines.usage import NoResolver, RecordedResolver
from server.fixtures import seed

FIXTURES = REPO_ROOT / "fixtures"


def dynatrace_env() -> DynatraceEnv:
    url = os.getenv("DT_PLATFORM_URL", "").strip().rstrip("/")
    token = os.getenv("DT_PLATFORM_TOKEN", "").strip()
    if not url or not token:
        raise SystemExit("set DT_PLATFORM_URL and DT_PLATFORM_TOKEN (see .env.example)")
    return DynatraceEnv(url, token, mcp_url=os.getenv("DT_MCP_URL", "").strip())


def _settings(sampling: int | None) -> dict[str, Any]:
    settings = json.loads((FIXTURES / "settings.json").read_text())["settings"]
    if sampling is not None:
        settings["discover"] = {**settings.get("discover", {}), "sampling_ratio": sampling}
    return settings


def report(snap: pipeline.Snapshot, settings: dict[str, Any], top: int) -> str:
    model = CostModel.from_settings(settings)
    by_parent: dict[str, int] = {}
    for r in snap.references:
        by_parent[r.signal_id] = by_parent.get(r.signal_id, 0) + 1
    rows = sorted(
        (s for s in snap.signals if s.parent_id is None),
        key=lambda s: -model.monthly(s.data_type, s.gib_day),
    )[:top]
    lines = [
        f"{'signal':58} {'GiB/day':>9} {'records/day':>13} {'€/month':>9} {'refs':>5}",
        *(
            f"{s.id[:58]:58} {s.gib_day:9.3f} {s.records_day:13,.0f} "
            f"{model.monthly(s.data_type, s.gib_day):9,.2f} {by_parent.get(s.id, 0):5d}"
            for s in rows
        ),
        "",
        f"{len(snap.signals)} signals, {len(snap.references)} references, "
        f"{len(snap.unresolved)} unresolved queries",
        f"{len(snap.queries)} DQL queries, {snap.scanned_bytes / GIB:.2f} GiB scanned",
        f"calibration (billed / estimated): {snap.calibration}",
        f"MCP tools: {', '.join(snap.mcp_tools) or '-'}",
    ]
    lines += [f"unresolved: {u.site.type} {u.site.name}: {u.reason}" for u in snap.unresolved]
    lines += [f"warning: {w}" for w in snap.warnings]
    return "\n".join(lines)


def cmd_discover(args: argparse.Namespace) -> int:
    settings = _settings(args.sampling)
    now = utcnow()
    if args.live:
        snap = pipeline.run(live_connectors(dynatrace_env()), settings, now, NoResolver())
    else:
        answers = json.loads((FIXTURES / "agent" / "usage_resolutions.json").read_text())
        conn = fixture_connectors(FIXTURES / "dynatrace", now)
        snap = pipeline.run(conn, settings, now, RecordedResolver(answers["answers"]))
    print(report(snap, settings, args.top))
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    from server.record import record

    env = dynatrace_env()
    out = Path(args.out).resolve()
    snap = record(env, _settings(args.sampling), out)
    print(f"recorded {len(snap.queries)} queries and the config into {out}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m server.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("reset", help="rebuild the demo database")
    d = sub.add_parser("discover", help="run discovery and print the top signals by cost")
    d.add_argument("--live", action="store_true", help="read the tenant in DT_PLATFORM_URL")
    d.add_argument("--top", type=int, default=10)
    d.add_argument("--sampling", type=int, default=None, help="fetch samplingRatio (1 = none)")
    r = sub.add_parser("record", help="record live responses into fixtures/dynatrace")
    r.add_argument("--sampling", type=int, default=1)
    r.add_argument("--out", default=str(FIXTURES / "dynatrace"))
    args = parser.parse_args(argv)
    if args.cmd == "reset":
        cfg = load_config()
        seed(make_engine(cfg.db_path), cfg, utcnow())
        print(f"Demo data reset: {cfg.db_path}")
        return 0
    if args.cmd == "discover":
        return cmd_discover(args)
    return cmd_record(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
