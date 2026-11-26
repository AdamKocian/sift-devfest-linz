"""Engines over connector data: planted cases, parser, patterns, discover, cost model."""

from __future__ import annotations

import dataclasses
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session

from server import engine, fixtures, synth
from server.config import REPO_ROOT, load_config
from server.connectors.dtconfig import QuerySite
from server.db import make_engine
from server.engines import cost, discover, dql_parser, ids, patterns, usage
from server.engines.discover import GIB, SignalDraft, Sizing
from server.engines.dql_parser import Variable, parse

WORLD = json.loads((REPO_ROOT / "fixtures" / "demo_world.json").read_text())
NOW = datetime(2026, 11, 26, 15, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> engine.World:
    """The seeded fixture world: signals and references come from the pipeline."""
    tmp = tmp_path_factory.mktemp("w")
    cfg = dataclasses.replace(
        load_config(), db_path=tmp / "sift.db", replay_speed=0, background_tasks=False
    )
    db = make_engine(cfg.db_path)
    fixtures.seed(db, cfg, NOW)
    with Session(db) as s:
        return engine.load_world(s, NOW)


def _case_signal(w: engine.World, n: int) -> str:
    """Signal id of planted case n, derived from demo_world.json the way discovery does."""
    for x in WORLD["logs"]:
        src = ids.log_source(x["service"], x["source"], x["level"])
        if x.get("case") == n:
            return src
        for p in x.get("patterns", []):
            if p.get("case") == n:
                return _pattern_child(w, src, p)
    for x in WORLD["spans"]:
        if x.get("case") == n:
            return ids.span(x["service"], x["name"])
    for x in WORLD["metrics"]:
        for d, c in (x.get("dimension_case") or {}).items():
            if c == n:
                return ids.metric_dimension(ids.metric(x["service"], x["key"]), d)
    raise KeyError(n)


def _pattern_child(w: engine.World, parent: str, p: dict[str, Any]) -> str:
    line = synth.render_line(p["lines"][0]["line"], random.Random(0))
    kids = [s for s in w.signals.values() if s.parent_id == parent]
    match = [k for k in kids if patterns.matches(patterns.phrase(k.key), line)]
    assert len(match) == 1, (parent, [k.key for k in kids])
    return match[0].id


def _guards(w: engine.World, sid: str) -> set[str]:
    return {g.rule or g.id for g in engine.guardrail_hits(w, w.signals[sid])}


# --- planted cases ----------------------------------------------------------------------


@pytest.mark.parametrize("case", [1, 2, 4, 5])
def test_cases_1_2_4_5_are_unused_and_unprotected(world: engine.World, case: int) -> None:
    sid = _case_signal(world, case)
    u = engine.usage(world, world.signals[sid])
    assert u.klass == "unused", (sid, u)
    assert u.by_type == {}
    assert _guards(world, sid) == set()


def test_case_3_duplicate_trace_is_only_queried_during_incidents(world: engine.World) -> None:
    sid = _case_signal(world, 3)
    assert world.signals[sid].kind == "log_pattern"
    u = engine.usage(world, world.signals[sid])
    # Two ad-hoc triage queries in 30 days, no dashboard, notebook, workflow or alert.
    assert (u.klass, u.queries_30d, u.by_type) == ("rarely", 2, {})
    assert _guards(world, sid) == set()


def test_case_5_dimension_is_unused_while_its_metric_is_used(world: engine.World) -> None:
    dim = world.signals[_case_signal(world, 5)]
    assert dim.kind == "metric_dimension" and dim.key == "user.id"
    metric = world.signals[dim.parent_id or ""]
    assert engine.usage(world, metric).klass == "used"
    readers = [r for r in world.refs[metric.id] if r.type in ("dashboard", "alert")]
    assert readers and all("user.id" not in r.fields for r in readers)
    assert dim.gib_day * 7 == pytest.approx(21.0, rel=1e-3)


def test_case_6_pii_logs_are_used_so_only_redaction_fits(world: engine.World) -> None:
    sid = _case_signal(world, 6)
    u = engine.usage(world, world.signals[sid])
    assert u.klass == "used" and u.by_type == {"notebook": 1}
    assert all("@" not in line for line in world.signals[sid].samples)  # examples redacted
    assert any("<email>" in line for line in world.signals[sid].samples)


def test_case_7_error_logs_hit_the_error_guardrail(world: engine.World) -> None:
    sid = _case_signal(world, 7)
    assert world.signals[sid].level == "ERROR"
    assert engine.usage(world, world.signals[sid]).klass == "unused"
    assert "error_levels" in _guards(world, sid)
    for child in world.children[sid]:  # the exception pattern inherits it
        assert "error_levels" in _guards(world, child)


def test_case_8_checkout_logs_are_used_and_protected(world: engine.World) -> None:
    sid = _case_signal(world, 8)
    u = engine.usage(world, world.signals[sid])
    assert u.klass == "used"
    assert u.by_type.get("dashboard") and u.by_type.get("alert")
    assert "referenced_90d" in _guards(world, sid)


def test_volumes_match_the_demo_world(world: engine.World) -> None:
    for x in WORLD["logs"]:
        sig = world.signals[ids.log_source(x["service"], x["source"], x["level"])]
        assert sig.gib_day * 7 == pytest.approx(x["gib_week"], rel=1e-6)
        assert sig.avg_record_bytes == pytest.approx(x["avg_record_bytes"], abs=1)
        assert sig.growth_wow == pytest.approx(x["growth_wow"], abs=1e-4)
    for x in WORLD["spans"]:
        sig = world.signals[ids.span(x["service"], x["name"])]
        assert sig.gib_day * 7 == pytest.approx(x["gib_week"], rel=1e-6)
    assert world.unattributed()["events"] * 7 == pytest.approx(WORLD["events_gib_week"])


def test_dashboard_variable_from_a_query_is_resolved_by_the_recorded_answer(
    world: engine.World,
) -> None:
    attr = world.signals[
        ids.span_attribute(ids.span("inventory", "SELECT inventory.stock"), "db.statement")
    ]
    readers = engine.affecting_refs(world, attr)
    assert [r.name for r in readers] == ["Inventory DB latency"]
    assert "Gemini" in (readers[0].note or "")


def test_fixtures_are_reproducible(tmp_path: Path) -> None:
    """`make fixtures-synth` regenerates exactly the committed recordings."""
    synth.main(out=tmp_path / "dynatrace", agent=tmp_path / "agent")
    committed = REPO_ROOT / "fixtures"
    for sub in ("dynatrace", "agent"):
        for f in sorted((tmp_path / sub).rglob("*.json")):
            rel = f.relative_to(tmp_path)
            assert f.read_text() == (committed / rel).read_text(), f"{rel} is stale"


# --- DQL parser -------------------------------------------------------------------------


def test_parser_filters_and_fields() -> None:
    q = parse(
        'fetch logs\n| filter service.name == "payment" and in(loglevel, {"INFO", "WARN"})\n'
        '| filterOut matchesPhrase(content, "healthz")\n| fields timestamp, content'
    )
    assert q.source == "logs" and q.projected and q.unresolved is None
    (t,) = q.terms
    assert t.eq == {"service.name": {"payment"}, "loglevel": {"INFO", "WARN"}}
    assert q.fields == {"service.name", "loglevel", "content", "timestamp"}


def test_parser_or_not_and_contradictions() -> None:
    q = parse(
        'fetch logs | filter (service.name == "a" or service.name == "b")'
        ' and not loglevel == "DEBUG"'
    )
    assert sorted(next(iter(t.eq["service.name"])) for t in q.terms) == ["a", "b"]
    assert all(t.ne == {"loglevel": {"DEBUG"}} for t in q.terms)
    q = parse('fetch logs | filter service.name == "a" and service.name == "b"')
    assert q.terms == []  # reads nothing


def test_parser_timeseries_metric_keys_and_dimensions() -> None:
    q = parse(
        "timeseries p95 = percentile(http.server.request.duration, 95), "
        'by:{service.name, http.route}, filter: service.name == "frontend"'
    )
    assert q.source == "metrics" and q.metric_keys == ["http.server.request.duration"]
    assert q.fields == {"service.name", "http.route"} and "p95" not in q.fields
    assert q.terms[0].eq == {"service.name": {"frontend"}}


def test_parser_variables() -> None:
    q = parse(
        "fetch spans | summarize c = count(), by:{$Attr:backtick}",
        {"Attr": Variable("Attr", ("db.statement",))},
    )
    assert q.fields == {"db.statement"}
    q = parse("fetch logs | filter loglevel == $L", {"L": Variable("L", ("INFO", "WARN"), True)})
    assert q.terms[0].eq == {"loglevel": {"INFO", "WARN"}}
    assert parse("fetch logs | filter loglevel == $Missing").unresolved == (
        "variable $Missing has no static value"
    )


@pytest.mark.parametrize(
    ("dql", "reason"),
    [
        ("fetch logs | lookup [fetch bizevents], sourceField:a, lookupField:b", "lookup"),
        ("fetch logs | append [fetch spans]", "append"),
        ("data record(a = 1)", "starts with `data`"),
        ("fetch logs | frobnicate x", "unknown command"),
        ('fetch logs | filter service.name == "x', "could not parse"),
    ],
)
def test_parser_reports_what_it_cannot_read(dql: str, reason: str) -> None:
    assert reason in (parse(dql).unresolved or "")


# --- usage attribution ------------------------------------------------------------------


def _draft(sid: str, kind: str, data_type: str, service: str, key: str, **kw: Any) -> SignalDraft:
    return SignalDraft(
        id=sid, kind=kind, data_type=data_type, service=service, key=key,
        est_bytes_day=GIB, records_day=1000, first_seen=NOW, **kw,
    )  # fmt: skip


def _catalog() -> usage.Catalog:
    src = _draft("src", "log_source", "logs", "cart", "app · WARN", source="app", level="WARN")
    pat = _draft(
        "pat", "log_pattern", "logs", "cart", "Timeout calling inventory <*>", parent_id="src"
    )
    err = _draft("err", "log_source", "logs", "cart", "app · ERROR", source="app", level="ERROR")
    return usage.Catalog(
        [src, pat, err],
        samples={"src": ["Timeout calling inventory x", "Retrying later"], "err": ["boom"]},
        templates={"pat": "Timeout calling inventory <*>"},
    )


def _site(dql: str, **kw: Any) -> usage.Site:
    return usage.Site("dashboard", "d1", "Board", "me", dql, count_30d=5, last_used=NOW, **kw)


class SpyResolver:
    def __init__(self, answer: usage.Resolution | None) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def resolve(
        self, site: usage.Site, reason: str, catalog: list[SignalDraft]
    ) -> usage.Resolution | None:
        self.calls.append((site.dql, reason))
        return self.answer


def test_phrase_on_a_pattern_is_attributed_to_the_pattern() -> None:
    r = usage.usage(
        [
            _site(
                'fetch logs | filter service.name == "cart"'
                ' and matchesPhrase(content, "Timeout calling")'
            )
        ],
        _catalog(),
        usage.NoResolver(),
    )
    assert [x.signal_id for x in r.references] == ["pat"]


def test_level_filter_narrows_and_missing_filter_reads_everything() -> None:
    r = usage.usage(
        [_site('fetch logs | filter loglevel == "ERROR"'), _site("fetch logs | limit 10")],
        _catalog(),
        usage.NoResolver(),
    )
    by_sig = {x.signal_id for x in r.references}
    assert by_sig == {"err", "src"}  # the second query reads every log source
    assert "*" in next(x for x in r.references if x.signal_id == "src").fields


def test_unresolved_queries_go_to_the_resolver() -> None:
    site = _site('fetch logs | filter dt.entity.host == "HOST-1"')
    spy = SpyResolver(usage.Resolution(("err",), ("content",), "Host runs cart.", "gemini-test"))
    r = usage.usage([site], _catalog(), spy)
    assert spy.calls and "dt.entity.host" in spy.calls[0][1]
    assert [(x.signal_id, x.resolved_by, x.note) for x in r.references] == [
        ("err", "gemini", "Host runs cart.")
    ]
    r = usage.usage([site], _catalog(), usage.NoResolver())
    assert r.references == [] and len(r.unresolved) == 1


def test_activity_is_matched_by_client_source_and_query_text() -> None:
    sites = usage.config_sites(
        [QuerySite("alert", "obj-1", "Errors", "team", "fetch logs | limit 1", 1.0)]
    )
    sites.append(_site("fetch spans | limit 1"))
    execs = [
        {"client.source": "obj-1", "query_string": "fetch logs | limit 1",
         "executions_30d": "43200", "last_run": "2026-11-26T14:59:00.000000000Z"},
        {"client.source": None, "query_string": "fetch logs\n| limit 5",
         "user.email": "a@b.example", "client.application_context": "dynatrace.notebooks",
         "executions_30d": 3, "last_run": "2026-11-20T10:00:00Z"},
    ]  # fmt: skip
    adhoc = usage.apply_activity(sites, execs, NOW)
    assert sites[0].count_30d == 43200
    assert sites[1].count_30d == 5  # untouched: no execution rows for it
    assert [(a.type, a.name, a.owner, a.count_30d) for a in adhoc] == [
        ("query", "Ad-hoc query · Notebooks", "a@b.example", 3)
    ]


def test_csv_variables_count_every_option() -> None:
    content = {
        "variables": [{"key": "L", "type": "csv", "input": "INFO,ERROR", "defaultValue": "INFO"}]
    }
    v = usage.dashboard_variables(content)["L"]
    assert v.values == ("INFO", "ERROR") and v.multiple


# --- patterns ---------------------------------------------------------------------------


def test_patterns_find_the_dominant_template_and_redact_examples() -> None:
    lines = [f"heartbeat ok - worker {i % 10} alive" for i in range(90)]
    lines += [
        f"Sent confirmation to user: u{i}@example.com order_id={100000 + i}" for i in range(10)
    ]
    mined = patterns.mine(lines)
    dom = patterns.dominant(mined)
    assert [patterns.display(t.template) for t in dom] == ["heartbeat ok - worker <*> alive"]
    assert patterns.phrase(dom[0].template) == "heartbeat ok"
    mail = next(t for t in mined if "confirmation" in t.template)
    assert all("@" not in e and "<redacted>" in e for e in mail.examples)


def test_a_source_with_one_template_has_no_pattern_signal() -> None:
    assert patterns.dominant(patterns.mine([f"Charge captured order={i}" for i in range(50)])) == []


def test_phrase_stops_at_punctuation_and_placeholders() -> None:
    assert patterns.phrase("Timeout calling inventory: SocketTimeoutException in <*>") == (
        "Timeout calling inventory"
    )
    assert patterns.matches("calling inventory", "Timeout calling inventory: x")
    assert not patterns.matches("inventory calling", "Timeout calling inventory: x")


# --- discover ---------------------------------------------------------------------------


def _day(k: int) -> str:
    d = NOW.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=k)
    return d.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")


def test_log_volume_rate_growth_and_calibration() -> None:
    rows = []
    for k in range(15):
        records = 1000 if k <= 7 else 800  # last 7 full days 1000/day, the week before 800
        rows.append({"service.name": "a", "log.source": "app", "loglevel": "INFO", "day": _day(k),
                     "records": str(records), "content_chars": records * 820})  # fmt: skip
    (sig,) = discover.log_sources(rows, [], NOW, Sizing(log_overhead_bytes=180))
    assert sig.records_day == 1000 and sig.est_bytes_day == 1000 * 1000
    assert sig.growth_wow == pytest.approx(0.25)
    factors, unattributed = discover.calibrate([sig], {"logs": 2_000_000, "events": GIB})
    assert factors["logs"] == pytest.approx(2.0) and sig.calibrated
    assert sig.avg_record_bytes == 2000
    assert unattributed == {"events": 1.0}


def test_new_signal_is_averaged_over_the_days_it_exists() -> None:
    rows = [{"service.name": "a", "log.source": "app", "loglevel": "INFO", "day": _day(k),
             "records": 700, "content_chars": 0} for k in (1, 2, 3)]  # fmt: skip
    (sig,) = discover.log_sources(rows, [], NOW, Sizing())
    assert sig.records_day == 700 and sig.growth_wow == 0


def test_without_billing_estimates_stay_uncalibrated() -> None:
    (m,) = discover.metrics([{"metric.key": "x", "service.name": "s", "series": 10}], NOW, Sizing())
    factors, _ = discover.calibrate([m], {})
    assert factors == {"metrics": None} and not m.calibrated
    assert m.records_day == 14_400


def test_dimension_signal_from_cardinality() -> None:
    (m,) = discover.metrics(
        [{"metric.key": "x", "service.name": "s", "series": 10_000}], NOW, Sizing()
    )
    dims = discover.metric_dimensions(
        m, {"series": 10_000, "without_user_id": 200, "without_http_route": 9000},
        ["http.route", "user.id"], Sizing(),
    )  # fmt: skip
    assert [(d.key, round(d.est_bytes_day / m.est_bytes_day, 2)) for d in dims] == [
        ("user.id", 0.98)
    ]


# --- cost model -------------------------------------------------------------------------


def test_all_in_cost_model_is_the_default() -> None:
    m = cost.CostModel.from_settings({"rates_eur_per_gib": {"logs": 2.4}})
    assert m.mode == "all_in"
    assert m.monthly("logs", 10) == pytest.approx(720)
    assert (
        m.saving("logs", 10, "openpipeline")
        == m.saving("logs", 10, "otelcol")
        == m.monthly("logs", 10)
    )


def test_split_cost_model_saves_only_retain_in_openpipeline() -> None:
    m = cost.CostModel.from_settings(
        {
            "rates_eur_per_gib": {"logs": 2.4},
            "cost_model": {
                "mode": "split",
                "split": {
                    "ingest_eur_per_gib": {"logs": 0.2},
                    "retain_eur_per_gib_day": {"logs": 0.001},
                    "retention_days": {"logs": 30},
                },
            },
        }
    )
    ingest, retain = 10 * 30 * 0.2, 10 * 30 * 0.001 * 30
    assert m.monthly("logs", 10) == pytest.approx(ingest + retain)
    assert m.saving("logs", 10, "openpipeline") == pytest.approx(retain)
    assert m.saving("logs", 10, "otelcol") == pytest.approx(ingest + retain)


def test_engine_savings_follow_the_cost_model(world: engine.World) -> None:
    p = world.proposal("p-payment-debug")
    assert p is not None
    assert engine.savings(world, p).eur_month == pytest.approx(828.0, abs=0.01)


def test_parse_variables_ignore_query_variables() -> None:
    content = {"variables": [{"key": "Q", "type": "query", "input": "fetch logs | fields a"}]}
    assert usage.dashboard_variables(content) == {}
    assert dql_parser.substitute("x == $Q", {})[1] == ["Q"]
