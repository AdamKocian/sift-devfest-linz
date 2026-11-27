"""Command line.

uv run python -m server.cli reset                 rebuild the demo database
uv run python -m server.cli discover [--live] [--top 10] [--sampling 1]
                                                  run discovery and print the top signals
uv run python -m server.cli record [--sampling 1] record live responses into fixtures/
uv run python -m server.cli agent analyze [--record]
uv run python -m server.cli agent chat "drop debug logs from payment but keep errors" [--record]
                                                  real Gemini run on Vertex AI over the
                                                  fixture data; --record writes fixtures/agent/
uv run python -m server.cli agent replay [analyze|chat]
                                                  offline: replay a recording, check it
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
    elif args.gemini_resolver:
        # Fixture data, but unresolved queries go to GEMINI_FAST_MODEL instead of the
        # recorded answers (one small call); prints the answer next to the recorded one.
        from server.agent.gemini import GeminiClient, Ledger
        from server.agent.resolver import GeminiResolver

        cfg = load_config()
        gemini = GeminiClient.vertex(
            cfg.gcp_project, cfg.gcp_location, cfg.gemini_model, cfg.gemini_fast_model
        )
        ledger = Ledger(max_tokens=50_000)
        resolver = GeminiResolver(gemini, ledger)
        conn = fixture_connectors(FIXTURES / "dynatrace", now)
        snap = pipeline.run(conn, settings, now, resolver)
        recorded = json.loads((FIXTURES / "agent" / "usage_resolutions.json").read_text())
        for key, a in resolver.cache.items():
            r = recorded["answers"].get(key, {})
            print(f"{a['site']}: gemini -> {a['signal_ids']} {a['fields']} ({a['model']})")
            print(f"  note: {a['note']}")
            print(f"  recorded -> {r.get('signal_ids')} {r.get('fields')}")
        for c in ledger.calls:
            print(
                f"  call: {c.served_model}, {c.tokens_in:,} in + {c.tokens_out:,} out, "
                f"${c.cost_usd or 0:.5f}, fallback: {c.fallback_reason or '-'}"
            )
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


def _print_log(session: Any, run_id: str | None, events: list[Any] | None = None) -> None:
    from sqlmodel import col, select

    from server.models import RunEvent

    rows = events
    if rows is None:
        rows = session.exec(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(col(RunEvent.seq))
        ).all()
    for e in rows:
        if e.type == "step":
            print(f"\n● {e.label}")
        elif e.type == "tool":
            ms = f" · {e.duration_ms / 1000:.1f} s" if e.duration_ms else ""
            rows_ = f" · {e.rows} rows" if e.rows is not None else ""
            print(f"    {e.label}{ms}{rows_}")
        elif e.type == "proposal":
            print(f"    → new proposal: {e.label}")
        elif e.type == "message":
            print(f"    agent: {e.label}")
        elif e.type == "llm":
            print(f"    · {e.label} · {(e.duration_ms or 0) / 1000:.1f} s")


def _agent_db(cfg: Any, name: str, history: bool) -> Any:
    import dataclasses

    db_path = cfg.db_path.parent / f"agent-{name}.db"
    db_path.unlink(missing_ok=True)
    cfg = dataclasses.replace(cfg, db_path=db_path)
    engine_ = make_engine(db_path)
    seed(engine_, cfg, utcnow(), history=history)
    return cfg, engine_


def cmd_agent(args: argparse.Namespace) -> int:
    import asyncio
    import dataclasses

    from sqlmodel import Session

    from server import engine as eng
    from server import fmt, runs
    from server.agent import prompts
    from server.agent import runner as ar
    from server.agent.gemini import GeminiClient
    from server.agent.tools import SiftTools
    from server.models import Run

    base = load_config()
    kind = args.kind
    if kind == "replay":
        return cmd_agent_replay(args)
    if not (base.gcp_project and base.gemini_model and base.gemini_fast_model):
        raise SystemExit("set GOOGLE_CLOUD_PROJECT, GEMINI_MODEL, GEMINI_FAST_MODEL (see .env)")
    state = "clean" if kind == "analyze" else "demo"
    cfg, db = _agent_db(base, kind, history=state == "demo")
    record_dir = FIXTURES / "dynatrace" if args.record else cfg.db_path.parent / "dry_runs"
    gemini = GeminiClient.vertex(
        cfg.gcp_project, cfg.gcp_location, cfg.gemini_model, cfg.gemini_fast_model
    )
    with Session(db, expire_on_commit=False) as session:
        start = utcnow()
        run = Run(
            id=f"run_rec_{kind}",
            type="analyze" if kind == "analyze" else "chat",
            status="running",
            trigger="user",
            started_at=start,
            scripted=False,
        )
        session.add(run)
        session.commit()
        log = ar.RunLog(session, run.id, utcnow)
        tools = SiftTools(
            session,
            runs._grail(cfg, start, record_dry_runs=record_dir),
            utcnow,
            run_id=run.id if kind == "analyze" else None,
            on_tool=log.tool,
            on_proposal=log.proposal,
            otelcol_bin=cfg.otelcol_bin,
            user_message=args.message if kind == "chat" else "",
            world_time=start,
        )
        mcp = (cfg.dt_mcp_url, cfg.dt_platform_token) if cfg.dt_mcp_url else None
        out = asyncio.run(
            ar.run_live(
                tools,
                gemini,
                model=cfg.gemini_model,
                instruction=prompts.ANALYZE if kind == "analyze" else prompts.CHAT,
                message=prompts.ANALYZE_TASK if kind == "analyze" else args.message,
                max_tokens=args.max_tokens or cfg.gemini_max_tokens_per_run,
                max_llm_calls=cfg.gemini_max_llm_calls,
                on_llm=log.llm,
                on_text=log.message,
                mcp=mcp,
                on_mcp=log.tool,
            )
        )
        log.finish()
        _print_log(session, run.id)
        w = eng.load_world(session, utcnow())
        print("\n--- proposals created")
        for pid in tools.created:
            p = w.proposal(pid)
            assert p is not None
            sv = eng.savings(w, p)
            print(f"  {p.id}: {p.title} · {p.action} · {p.finding_class} · "
                  f"{fmt.eur(sv.eur_month)}/month · reduction {p.reduction_ratio:.4f}")  # fmt: skip
        print("--- rejected by code")
        for r in tools.rejections:
            print(f"  [{r['kind']}] {r['title']}: {r['reason']}")
        lg = out.ledger
        print("--- Gemini usage")
        fb = f" (fallback: {lg.fallback_reason})" if lg.fallback_reason else ""
        cached = sum(c.cached for c in lg.calls)
        print(f"  requested {lg.requested_model}, served by {lg.served_model}{fb}")
        print(
            f"  {len(lg.calls)} calls, {lg.tokens_in:,} input ({cached:,} cached) + "
            f"{lg.tokens_out:,} output tokens, est. ${lg.cost_usd or 0:.4f}"
        )
        print(f"  models per call: {[c.served_model for c in lg.calls]}")
        print(f"  Dynatrace MCP tools called: {out.mcp_tools or 'none (DT_MCP_URL not set)'}")
        print("--- final text" + (" (numbers checked: ok)" if not out.unbacked else
              f" (numbers without source: {out.unbacked})"))  # fmt: skip
        print(f"  {out.final_text}")
        if args.record:
            name = "analyze" if kind == "analyze" else "chat"
            extra = {"message": args.message} if kind == "chat" else {}
            rec = ar.recording(out, kind, state, start, **extra)
            path = FIXTURES / "agent" / f"{name}.json"
            path.write_text(json.dumps(rec, indent=1, ensure_ascii=False, default=str) + "\n")
            print(f"\nrecorded {path.relative_to(REPO_ROOT)}")
            for f in prune_dry_runs():
                print(f"removed unused {f.relative_to(REPO_ROOT)}")
    del dataclasses
    return 0


class _Tracking:
    """A GrailClient wrapper that remembers which queries ran."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.guard, self.names = inner, inner.guard, set()

    def query(self, q: Any) -> Any:
        self.names.add(q.name)
        return self.inner.query(q)


def _replay(name: str, grail_wrap: Any = None) -> tuple[Any, Any]:
    from sqlmodel import Session

    from server import runs
    from server.agent import runner as ar

    rec = ar.load_recording(FIXTURES / "agent" / f"{name}.json")
    if rec is None:
        raise SystemExit(f"no recording fixtures/agent/{name}.json")
    cfg, db = _agent_db(load_config(), f"replay-{name}", history=rec["state"] == "demo")
    session = Session(db, expire_on_commit=False)
    start = utcnow()
    grail = runs._grail(cfg, start)
    if grail_wrap is not None:
        grail = grail_wrap(grail)
    log = ar.RunLog(session, "replay", lambda: start)
    gen = ar.replay(
        session, grail, lambda: start, rec,
        run_id="replay" if rec["kind"] == "analyze" else None, log=log,
        user_message=rec.get("message", ""),
    )  # fmt: skip
    out = ar.run_replay_sync(gen, lambda ms: None)
    log.finish()
    return out, session


def prune_dry_runs() -> list[Path]:
    """Remove dry-run recordings no agent recording uses (drafts of an earlier attempt).
    Finds the used ones by replaying every recording offline."""
    from server.connectors.store import safe_name

    used: set[str] = set()
    for rec_path in sorted((FIXTURES / "agent").glob("*.json")):
        rec = json.loads(rec_path.read_text())
        if not isinstance(rec, dict) or "calls" not in rec:
            continue
        holder: list[_Tracking] = []

        def wrap(g: Any, holder: list[_Tracking] = holder) -> _Tracking:
            holder.append(_Tracking(g))
            return holder[0]

        _replay(rec_path.stem, wrap)
        used |= {safe_name(n) for n in holder[0].names}
    removed = []
    for f in sorted((FIXTURES / "dynatrace" / "grail").glob("dry_run_*.json")):
        if f.stem not in used:
            f.unlink()
            removed.append(f)
    return removed


def cmd_agent_replay(args: argparse.Namespace) -> int:
    """Replay a recording offline on the state it was recorded in, and check it."""
    name = args.message or "analyze"
    out, session = _replay(name)
    _print_log(session, "replay")
    print(f"\ndiverged from the recording: {out.diverged}")
    print(f"numbers without a source: {out.unbacked or 'none'}")
    print(f"summary: {out.summary}")
    if args.record:  # `agent replay --record`: also drop dry runs nothing uses
        for f in prune_dry_runs():
            print(f"removed unused {f.relative_to(REPO_ROOT)}")
    return 1 if out.diverged or out.unbacked else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m server.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("reset", help="rebuild the demo database")
    d = sub.add_parser("discover", help="run discovery and print the top signals by cost")
    d.add_argument("--live", action="store_true", help="read the tenant in DT_PLATFORM_URL")
    d.add_argument("--top", type=int, default=10)
    d.add_argument("--sampling", type=int, default=None, help="fetch samplingRatio (1 = none)")
    d.add_argument(
        "--gemini-resolver", action="store_true", help="resolve unparsed queries with Gemini"
    )
    r = sub.add_parser("record", help="record live responses into fixtures/dynatrace")
    r.add_argument("--sampling", type=int, default=1)
    r.add_argument("--out", default=str(FIXTURES / "dynatrace"))
    a = sub.add_parser("agent", help="real Gemini agent run over the fixture data")
    a.add_argument("kind", choices=["analyze", "chat", "replay"])
    a.add_argument("message", nargs="?", default="", help="chat message / recording to replay")
    a.add_argument("--record", action="store_true", help="write fixtures/agent/<kind>.json")
    a.add_argument("--max-tokens", type=int, default=0, help="token budget for this run")
    args = parser.parse_args(argv)
    if args.cmd == "reset":
        cfg = load_config()
        seed(make_engine(cfg.db_path), cfg, utcnow())
        print(f"Demo data reset: {cfg.db_path}")
        return 0
    if args.cmd == "discover":
        return cmd_discover(args)
    if args.cmd == "agent":
        return cmd_agent(args)
    return cmd_record(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
