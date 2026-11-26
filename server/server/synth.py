"""`make fixtures-synth`: turn fixtures/demo_world.json into synthetic recordings.

Until a tenant exists, fixtures/dynatrace/ is produced here: a `SpecGrail` answers every
query the real pipeline asks with what Grail would return for the demo world, and each
answer is written exactly where the fixture connectors read it. Documents, workflows,
custom alerts, SLOs and the MCP tool list are written in their API response shapes.
`make fixtures` (server.record) overwrites the same files from a live tenant.

The demo world is synthetic: shapes follow the official API docs (links in the
connectors), values come from docs/DEMO_WORLD.md."""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from server import dql
from server.config import REPO_ROOT
from server.connectors import Connectors
from server.connectors.documents import FixtureDocumentsClient
from server.connectors.dtconfig import FixtureConfigClient
from server.connectors.grail import CostGuard, Query, QueryResult, parse_response
from server.connectors.mcp_client import FixtureDynatraceMcp
from server.connectors.store import FixtureStore, format_ts, parse_ts, safe_name
from server.dql import DiscoverParams
from server.engines import ids, patterns, pipeline
from server.engines.discover import GIB, Sizing
from server.engines.dql_parser import Variable, substitute
from server.engines.usage import RecordedResolver, dashboard_variables, query_key

ENV_HOST = "https://abc12345.apps.dynatrace.com"
FIXTURES = REPO_ROOT / "fixtures"


def offset(text: str, base: datetime) -> datetime:
    m = re.fullmatch(r"-(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?", text)
    if not m:
        raise ValueError(f"bad offset {text!r}")
    d, h, mi = (int(x or 0) for x in m.groups())
    return base - timedelta(days=d, hours=h, minutes=mi)


# --- sample lines -----------------------------------------------------------------------

_PH = re.compile(r"\{(\w+)(?::([^}]*))?\}")
NAMES = ("alice", "bob", "carla", "deniz", "emil", "fatima", "georg", "hana", "ivan", "jana")


def render_line(template: str, rng: random.Random) -> str:
    trace = ""

    def sub(m: re.Match[str]) -> str:
        nonlocal trace
        kind, arg = m.group(1), m.group(2) or ""
        if kind == "int" or kind == "ms":
            lo, hi = (int(x) for x in arg.split("-"))
            return f"{rng.randint(lo, hi)}" + ("ms" if kind == "ms" else "")
        if kind == "hex":
            return "".join(rng.choice("0123456789abcdef") for _ in range(int(arg)))
        if kind == "uuid":
            return str(uuid.UUID(int=rng.getrandbits(128), version=4))
        if kind == "email":
            return f"{rng.choice(NAMES)}{rng.randint(1, 99)}@example.com"
        if kind == "choice":
            return rng.choice(arg.split("|"))
        if kind == "trace":
            frames = [
                f"\tat shop.{rng.choice(['cart', 'inventory', 'net'])}.Frame{i}.call(Frame{i}.java:{rng.randint(10, 400)})"
                for i in range(int(arg))
            ]
            trace = "\n" + "\n".join(frames)
            return ""
        raise ValueError(f"unknown placeholder {kind}")

    return _PH.sub(sub, template) + trace


def weighted_lines(spec: list[dict[str, Any]], n: int, rng: random.Random) -> list[str]:
    total = sum(x["weight"] for x in spec)
    out: list[str] = []
    for i, x in enumerate(spec):
        k = round(n * x["weight"] / total) if i < len(spec) - 1 else n - len(out)
        out.extend(render_line(x["line"], rng) for _ in range(max(k, 0)))
    return out


# --- the world as numbers ---------------------------------------------------------------


@dataclass
class Series:
    """Per-day values for days_ago 0 (today, partial) .. 14."""

    rate: float  # per full day in the last 7 days
    growth: float
    today_fraction: float

    def day(self, k: int) -> float:
        weights = [(1 + self.growth) ** (-j / 7) for j in range(1, 8)]
        c = 7 / sum(weights)
        if k == 0:
            return self.rate * c * self.today_fraction
        return self.rate * c * (1 + self.growth) ** (-k / 7)


class World:
    def __init__(self, spec: dict[str, Any], sizing: Sizing, params: DiscoverParams) -> None:
        self.spec = spec
        self.sizing = sizing
        self.params = params
        self.at = parse_ts(spec["recorded_at"])
        midnight = self.at.replace(hour=0, minute=0, second=0, microsecond=0)
        self.midnight = midnight
        self.today_fraction = (self.at - midnight).total_seconds() / 86400
        self.logs = {ids.log_source(x["service"], x["source"], x["level"]): x for x in spec["logs"]}
        self.rng = random.Random(spec["seed"])

    def day_start(self, k: int) -> datetime:
        return self.midnight - timedelta(days=k)

    def series(self, gib_week: float, growth: float) -> Series:
        return Series(gib_week / 7, growth, self.today_fraction)

    # logs
    def log_records_bytes(
        self, x: dict[str, Any], gib_week: float, avg: int
    ) -> tuple[Series, Series]:
        g = x.get("growth_wow", 0.0)
        rec = self.series(gib_week * GIB / avg, g)
        byt = self.series(gib_week * GIB, g)
        return rec, byt

    def first_seen(self, text: str) -> datetime:
        floor = self.at - timedelta(days=self.params.first_seen_days) + timedelta(hours=1)
        return max(offset(text, self.at), floor)


# --- Grail responses --------------------------------------------------------------------


def _type(v: Any) -> str:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "long"
    if isinstance(v, float):
        return "double"
    if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}T", v):
        return "timestamp"
    return "string"


def grail_body(
    q: Query, records: list[dict[str, Any]], scanned: float, at: datetime, ratio: int = 1
) -> dict[str, Any]:
    mappings: dict[str, dict[str, str]] = {}
    for r in records:
        for k, v in r.items():
            if v is not None:
                mappings.setdefault(k, {"type": _type(v)})
    out_records = [
        {k: (str(v) if isinstance(v, int) and not isinstance(v, bool) else v) for k, v in r.items()}
        for r in records
    ]  # Grail serialises longs as strings
    qid = str(uuid.UUID(hashlib.md5(q.dql.encode()).hexdigest()))
    return {
        "state": "SUCCEEDED",
        "progress": 100,
        "ttlSeconds": 500,
        "result": {
            "records": out_records,
            "types": [{"indexRange": [0, max(len(records) - 1, 0)], "mappings": mappings}],
            "metadata": {
                "grail": {
                    "canonicalQuery": q.dql,
                    "timezone": "Z",
                    "query": q.dql,
                    "scannedRecords": int(scanned / 400),
                    "dqlVersion": "V1_0",
                    "scannedBytes": int(scanned),
                    "scannedDataPoints": 0,
                    "analysisTimeframe": {
                        "start": format_ts(at - timedelta(days=15)),
                        "end": format_ts(at),
                    },
                    "locale": "und",
                    "executionTimeMilliseconds": 180 + int(scanned / GIB * 40),
                    "notifications": [],
                    "queryId": qid,
                    "sampled": ratio > 1,
                }
            },
        },
    }


class SpecGrail:
    """Answers the pipeline's queries from the demo world and records each answer."""

    def __init__(self, world: World, store: FixtureStore) -> None:
        self.w = world
        self.store = store
        self.guard = CostGuard()

    def query(self, q: Query) -> QueryResult:
        self.guard.check(q)
        kind, _, arg = q.name.partition(":")
        records, scanned, ratio = getattr(self, f"_{kind}")(q, arg)
        body = grail_body(q, records, scanned, self.w.at, ratio)
        self.store.write(
            {"name": q.name, "query": q.dql, "recorded_at": format_ts(self.w.at), "response": body},
            "grail",
            f"{safe_name(q.name)}.json",
        )
        result = parse_response(body)
        self.guard.record(q, result)
        return result

    # discover
    def _billing_ingest(self, q: Query, _: str) -> tuple[list[dict[str, Any]], float, int]:
        w, s = self.w, self.w.sizing
        rows = []
        for k in range(15):
            logs = sum(
                self.w.log_records_bytes(x, x["gib_week"], x["avg_record_bytes"])[1].day(k)
                for x in w.spec["logs"]
            )
            spans = sum(
                w.series(x["gib_week"] * GIB, x.get("growth_wow", 0.0)).day(k)
                for x in w.spec["spans"]
            )
            events = w.series(w.spec["events_gib_week"] * GIB, 0.0).day(k)
            for t, v in zip(dql.BILLING_INGEST_TYPES, (logs, spans, events), strict=True):
                rows.append(
                    {
                        "event.type": t,
                        "day": format_ts(w.day_start(k)),
                        "billed_bytes": int(round(v)),
                    }
                )
        del s
        return rows, 15 * 2_000_000, 1

    def _log_volume(self, q: Query, _: str) -> tuple[list[dict[str, Any]], float, int]:
        w, oh = self.w, self.w.sizing.log_overhead_bytes
        rows, total = [], 0.0
        for x in w.spec["logs"]:
            rec, byt = w.log_records_bytes(x, x["gib_week"], x["avg_record_bytes"])
            for k in range(15):
                n, b = rec.day(k), byt.day(k)
                total += b
                rows.append({
                    "service.name": x["service"], "log.source": x["source"], "loglevel": x["level"],
                    "day": format_ts(w.day_start(k)), "records": int(round(n)),
                    "content_chars": int(round(b - round(n) * oh)),
                })  # fmt: skip
        return rows, total / w.params.sampling_ratio, w.params.sampling_ratio

    def _log_first_seen(self, q: Query, _: str) -> tuple[list[dict[str, Any]], float, int]:
        w = self.w
        rows = [
            {"service.name": x["service"], "log.source": x["source"], "loglevel": x["level"],
             "first_seen": format_ts(w.first_seen(x["first_seen"]))}
            for x in w.spec["logs"]
        ]  # fmt: skip
        total = sum(x["gib_week"] for x in w.spec["logs"]) / 7 * GIB * w.params.first_seen_days
        return rows, total / 1000, 1000

    def _span_volume(self, q: Query, _: str) -> tuple[list[dict[str, Any]], float, int]:
        w, base = self.w, self.w.sizing.span_base_bytes
        rows, total = [], 0.0
        for x in w.spec["spans"]:
            g = x.get("growth_wow", 0.0)
            attrs = x.get("attributes", {})
            attr_gib = sum(a["gib_week"] for a in attrs.values())
            rec = w.series((x["gib_week"] - attr_gib) * GIB / base, g)
            for k in range(15):
                row: dict[str, Any] = {
                    "service.name": x["service"], "span.name": x["name"],
                    "day": format_ts(w.day_start(k)), "records": int(round(rec.day(k))),
                }  # fmt: skip
                for a in w.params.span_attributes:
                    chars = w.series(attrs[a]["gib_week"] * GIB, g).day(k) if a in attrs else 0.0
                    row[f"attr_{dql.column(a)}"] = int(round(chars))
                total += w.series(x["gib_week"] * GIB, g).day(k)
                rows.append(row)
        return rows, total / w.params.sampling_ratio, w.params.sampling_ratio

    def _span_first_seen(self, q: Query, _: str) -> tuple[list[dict[str, Any]], float, int]:
        w = self.w
        rows = [
            {"service.name": x["service"], "span.name": x["name"],
             "first_seen": format_ts(w.first_seen(x["first_seen"]))}
            for x in w.spec["spans"]
        ]  # fmt: skip
        total = sum(x["gib_week"] for x in w.spec["spans"]) / 7 * GIB * w.params.first_seen_days
        return rows, total / 1000, 1000

    def _series_count(self, x: dict[str, Any]) -> int:
        s = self.w.sizing
        return round(
            x["gib_week"] / 7 * GIB / (s.metric_points_per_series_day * s.metric_bytes_per_point)
        )

    def _metric_series(self, q: Query, _: str) -> tuple[list[dict[str, Any]], float, int]:
        rows = [
            {"metric.key": x["key"], "service.name": x["service"], "series": self._series_count(x)}
            for x in self.w.spec["metrics"]
        ]
        return rows, 64 * 1024**2, 1

    def _metric(self, key: str) -> dict[str, Any]:
        return next(x for x in self.w.spec["metrics"] if x["key"] == key)

    def _metric_dimensions(self, q: Query, key: str) -> tuple[list[dict[str, Any]], float, int]:
        x = self._metric(key)
        row: dict[str, Any] = {"metric.key": key}
        for d in x["dimensions"]:
            row[d] = x["service"] if d == "service.name" else f"<{d}>"
        return [row], 8 * 1024**2, 1

    def _metric_cardinality(self, q: Query, key: str) -> tuple[list[dict[str, Any]], float, int]:
        x = self._metric(key)
        total = self._series_count(x)
        row: dict[str, Any] = {"series": total}
        shares = x.get("dimension_share", {})
        for d in x["dimensions"]:
            row[f"without_{dql.column(d)}"] = round(total * (1 - shares.get(d, 0.0)))
        return [row], 32 * 1024**2, 1

    # patterns
    def _source_lines(self, sid: str, n: int) -> list[str]:
        x = self.w.logs[sid]
        parts = [(x["avg_record_bytes"], x["gib_week"], x["lines"])]
        rest_gib = x["gib_week"]
        rest_rec = x["gib_week"] / x["avg_record_bytes"]
        for p in x.get("patterns", []):
            rest_gib -= p["gib_week"]
            rest_rec -= p["gib_week"] / p["avg_record_bytes"]
        mix = [(rest_rec, x["lines"])] + [
            (p["gib_week"] / p["avg_record_bytes"], p["lines"]) for p in x.get("patterns", [])
        ]
        del parts, rest_gib
        total = sum(r for r, _ in mix)
        rng = random.Random(f"{self.w.spec['seed']}:{sid}")
        out: list[str] = []
        for r, spec in mix:
            out.extend(weighted_lines(spec, round(n * r / total), rng))
        rng.shuffle(out)
        return out

    def _log_sample(self, q: Query, sid: str) -> tuple[list[dict[str, Any]], float, int]:
        n = min(int(re.search(r"limit (\d+)", q.dql).group(1)), 200)  # type: ignore[union-attr]
        lines = self._source_lines(sid, n)
        ratio = int(m.group(1)) if (m := re.search(r"samplingRatio: (\d+)", q.dql)) else 1
        x = self.w.logs[sid]
        return [{"content": c} for c in lines], x["gib_week"] / 7 * GIB / ratio, ratio

    def _pattern_volume(self, q: Query, sid: str) -> tuple[list[dict[str, Any]], float, int]:
        x = self.w.logs[sid]
        oh = self.w.sizing.log_overhead_bytes
        pairs = re.findall(r'matchesPhrase\(content, "((?:\\.|[^"\\])*)"\), "([^"]+)"', q.dql)
        rng = random.Random(1)
        agg: dict[str, list[float]] = {}
        rest_rec = x["gib_week"] * GIB / x["avg_record_bytes"]
        rest_bytes = x["gib_week"] * GIB
        for p in x.get("patterns", []):
            line = render_line(p["lines"][0]["line"], rng)
            pid = next((pid for phrase, pid in pairs if patterns.matches(phrase, line)), "other")
            n = p["gib_week"] * GIB / p["avg_record_bytes"]
            b = p["gib_week"] * GIB
            a = agg.setdefault(pid, [0.0, 0.0])
            a[0] += n
            a[1] += b
            rest_rec -= n
            rest_bytes -= b
        o = agg.setdefault("other", [0.0, 0.0])
        o[0] += rest_rec
        o[1] += rest_bytes
        rows = [
            {
                "sift.pattern": pid,
                "records": int(round(n)),
                "content_chars": int(round(b - round(n) * oh)),
            }
            for pid, (n, b) in sorted(agg.items())
        ]
        return (
            rows,
            x["gib_week"] * GIB / self.w.params.sampling_ratio,
            self.w.params.sampling_ratio,
        )

    # usage
    def _query_executions(self, q: Query, _: str) -> tuple[list[dict[str, Any]], float, int]:
        w, at = self.w, self.w.at
        rows: list[dict[str, Any]] = []

        def row(text: str, app: str, source: str | None, user: str, n: int, last: str) -> None:
            rows.append({
                "query_string": text, "client.application_context": app, "client.source": source,
                "user.email": user, "executions_30d": n, "last_run": format_ts(offset(last, at)),
            })  # fmt: skip

        for d in w.spec["dashboards"]:
            content = dashboard_content(d)
            variables = dashboard_variables(content)
            for v in d.get("variables", []):
                if v["type"] == "query":  # what the variable resolved to when the tile ran
                    variables[v["key"]] = Variable(v["key"], ("db.statement",))
            src = f"{ENV_HOST}/ui/apps/dynatrace.dashboards/dashboard/{d['id']}"
            for t in d["tiles"]:
                text = substitute(t["query"], variables)[0]
                row(
                    text,
                    "dynatrace.dashboards",
                    src,
                    f"{d['owner']}@noisy-shop.example",
                    d["executions_30d"],
                    d["last_run"],
                )
        for nb in w.spec["notebooks"]:
            src = f"{ENV_HOST}/ui/apps/dynatrace.notebooks/notebook/{nb['id']}"
            for s in nb["sections"]:
                row(
                    s["query"],
                    "dynatrace.notebooks",
                    src,
                    f"{nb['owner']}@noisy-shop.example",
                    nb["executions_30d"],
                    nb["last_run"],
                )
        for wf in w.spec["workflows"]:
            for t in wf["tasks"]:
                row(
                    t["query"],
                    "dynatrace.automations",
                    f"dynatrace.automations:{wf['id']}",
                    "automation",
                    wf["executions_30d"],
                    wf["last_run"],
                )
        for a in w.spec["alerts"]:
            row(
                a["query"],
                "dynatrace.davis.anomaly-detection",
                a["id"],
                "davis",
                a["executions_30d"],
                a["last_run"],
            )
        for s in w.spec["slos"]:
            # TODO(verify): the client.source an SLO evaluation records.
            row(
                s["indicator"],
                "dynatrace.site.reliability.guardian",
                f"dynatrace.slo:{s['id']}",
                "slo",
                s["executions_30d"],
                s["last_run"],
            )
        for x in w.spec["adhoc"]:
            row(x["query"], x["app"], None, x["user"], x["executions_30d"], x["last_run"])
        return rows, 12 * 1024**2, 1


# --- documents and config ---------------------------------------------------------------


def dashboard_content(d: dict[str, Any]) -> dict[str, Any]:
    tiles, layouts = {}, {}
    for i, t in enumerate(d["tiles"], start=1):
        tiles[str(i)] = {
            "type": "data", "title": t["title"], "query": t["query"],
            "visualization": "lineChart" if "bin(" in t["query"] or t["query"].startswith("timeseries") else "table",
            "visualizationSettings": {}, "querySettings": {},
        }  # fmt: skip
        layouts[str(i)] = {"x": 0 if i % 2 else 12, "y": (i - 1) // 2 * 8, "w": 12, "h": 8}
    variables = [
        {"version": 2, "visible": True, "editable": True, **v} for v in d.get("variables", [])
    ]
    return {"version": 21, "variables": variables, "tiles": tiles, "layouts": layouts}


def notebook_content(nb: dict[str, Any]) -> dict[str, Any]:
    sections = []
    for i, s in enumerate(nb["sections"]):
        sections.append({
            "id": str(uuid.UUID(hashlib.md5(f"{nb['id']}:{i}".encode()).hexdigest())),
            "type": "dql", "title": s["title"], "showTitle": True,
            "state": {
                "input": {"value": s["query"]},
                "visualization": "table",
                "visualizationSettings": {"autoSelectVisualization": True},
                "querySettings": {"maxResultRecords": 1000, "defaultScanLimitGbytes": 500,
                                  "maxResultMegaBytes": 1, "defaultSamplingRatio": 10, "enableSampling": False},
            },
        })  # fmt: skip
    return {
        "version": "7",
        "defaultTimeframe": {"from": "now()-2h", "to": "now()"},
        "defaultSegments": [],
        "sections": sections,
    }


SYNTHETIC = (
    "Synthetic recording made by `make fixtures-synth` from fixtures/demo_world.json in the "
    "documented response shape; replace with `make fixtures` once a tenant exists."
)


def write_documents(w: World, store: FixtureStore) -> None:
    at = format_ts(w.at)
    docs = []
    for kind, items, build in (
        ("dashboard", w.spec["dashboards"], dashboard_content),
        ("notebook", w.spec["notebooks"], notebook_content),
    ):
        for d in items:
            meta = {
                "id": d["id"], "name": d["name"], "type": kind, "version": 3, "owner": d["owner"],
                "isPrivate": False,
                "modificationInfo": {"createdTime": format_ts(w.at - timedelta(days=120)), "createdBy": d["owner"],
                                     "lastModifiedTime": format_ts(w.at - timedelta(days=9)), "lastModifiedBy": d["owner"]},
            }  # fmt: skip
            docs.append(meta)
            store.write({"_about": SYNTHETIC, "recorded_at": at, "metadata": meta, "content": build(d)},
                        "documents", f"{safe_name(d['id'])}.json")  # fmt: skip
    page = {"documents": docs, "totalCount": len(docs), "nextPageKey": None}
    store.write({"_about": SYNTHETIC, "recorded_at": at, "pages": [page]}, "documents", "list.json")


def write_config(w: World, store: FixtureStore) -> None:
    at = format_ts(w.at)
    results = []
    for wf in w.spec["workflows"]:
        tasks = {
            t["name"]: {"name": t["name"], "action": "dynatrace.automations:execute-dql-query",
                        "description": "", "input": {"query": t["query"]}, "active": True, "position": {"x": 0, "y": 1}}
            for t in wf["tasks"]
        }  # fmt: skip
        sched = {"isActive": True, "isFaulty": False, "trigger": wf["schedule"], "timezone": "UTC"}
        results.append({
            "id": wf["id"], "title": wf["title"], "owner": wf["owner"], "ownerType": "USER", "isPrivate": False,
            "isDeployed": True, "triggerType": "Schedule", "trigger": {"schedule": sched}, "tasks": tasks,
        })  # fmt: skip
    store.write({"_about": SYNTHETIC, "recorded_at": at, "response": {"count": len(results), "results": results}},
                "automation", "workflows.json")  # fmt: skip
    items = [
        {"objectId": a["id"], "owner": a["owner"],
         "value": {"enabled": True, "title": a["title"], "source": {"query": a["query"], "interval": a["interval"]}}}
        for a in w.spec["alerts"]
    ]  # fmt: skip
    store.write({"_about": SYNTHETIC + " TODO(verify): schema id and value fields of custom alerts.",
                 "recorded_at": at, "response": {"items": items, "totalCount": len(items), "pageSize": 500}},
                "settings", "custom-alerts.json")  # fmt: skip
    slos = [
        {"id": s["id"], "name": s["name"], "owner": s["owner"], "version": "1", "customSli": {"indicator": s["indicator"]},
         "criteria": [{"timeframeFrom": "now-7d", "timeframeTo": "now", "target": 99.0}], "tags": []}
        for s in w.spec["slos"]
    ]  # fmt: skip
    store.write({"_about": SYNTHETIC + " TODO(verify): customSli.indicator.", "recorded_at": at,
                 "response": {"totalCount": len(slos), "nextPageKey": None, "slos": slos}},
                "slo", "slos.json")  # fmt: skip
    tools = [
        ("execute_dql", "Run a DQL statement on Grail."),
        ("verify_dql", "Check a DQL statement before running it."),
        ("generate_dql_from_natural_language", "Turn a question into DQL."),
        ("explain_dql_in_natural_language", "Explain a DQL statement."),
        ("list_problems", "List Davis problems."),
        ("find_entity_by_name", "Find a monitored entity by name."),
        ("list_documents", "List dashboards and notebooks."),
        ("read_document", "Read a dashboard or notebook."),
    ]
    store.write({"_about": SYNTHETIC + " Tool names from the archived dynatrace-oss/dynatrace-mcp README; "
                 "the remote server's list comes from tools/list.", "recorded_at": at,
                 "response": {"tools": [{"name": n, "description": d, "inputSchema": {"type": "object"}} for n, d in tools]}},
                "mcp", "tools.json")  # fmt: skip
    store.write(
        {
            "recorded_at": at,
            "id": w.spec["environment"]["id"],
            "name": w.spec["environment"]["name"],
        },
        "environment.json",
    )


def write_resolutions(w: World) -> dict[str, dict[str, Any]]:
    answers: dict[str, dict[str, Any]] = {}
    for r in w.spec["gemini_resolutions"]:
        dash_name, _, tile_title = r["site"].partition(" / ")
        d = next(x for x in w.spec["dashboards"] if x["name"] == dash_name)
        t = next(x for x in d["tiles"] if x["title"] == tile_title)
        sigs = [ids.span(s["service"], s["name"]) for s in r["signals"]]
        answers[query_key(t["query"])] = {
            "site": r["site"], "query": t["query"], "signal_ids": sigs,
            "fields": r["fields"], "note": r["note"], "model": r["model"],
        }  # fmt: skip
    return answers


def write_findings(w: World, snap: pipeline.Snapshot) -> dict[str, dict[str, Any]]:
    """The recorded agent assessment (phase 03 replaces it with live Gemini output)."""
    out: dict[str, dict[str, Any]] = {}
    by_parent: dict[str, list[Any]] = {}
    for s in snap.signals:
        if s.parent_id:
            by_parent.setdefault(s.parent_id, []).append(s)
    rng = random.Random(1)
    for x in w.spec["logs"]:
        sid = ids.log_source(x["service"], x["source"], x["level"])
        if x.get("finding"):
            out[sid] = x["finding"]
        for p in x.get("patterns", []):
            line = render_line(p["lines"][0]["line"], rng)
            match = [
                c for c in by_parent.get(sid, []) if patterns.matches(patterns.phrase(c.key), line)
            ]
            if p.get("finding") and match:
                out[match[0].id] = p["finding"]
    for x in w.spec["spans"]:
        if x.get("finding"):
            out[ids.span(x["service"], x["name"])] = x["finding"]
    for x in w.spec["metrics"]:
        mid = ids.metric(x["service"], x["key"])
        for d, f in (x.get("dimension_finding") or {}).items():
            out[ids.metric_dimension(mid, d)] = f
    return out


def main(
    spec_path: Path = FIXTURES / "demo_world.json",
    out: Path = FIXTURES / "dynatrace",
    agent: Path = FIXTURES / "agent",
) -> pipeline.Snapshot:
    spec = json.loads(spec_path.read_text())
    settings = json.loads((FIXTURES / "settings.json").read_text())["settings"]
    w = World(
        spec,
        Sizing.from_settings(settings.get("discover")),
        DiscoverParams.from_settings(settings.get("discover")),
    )
    if out.exists():
        shutil.rmtree(out)
    store = FixtureStore(out)  # no time shift while writing
    write_documents(w, store)
    write_config(w, store)
    answers = write_resolutions(w)
    conn = Connectors(
        grail=SpecGrail(w, store),
        documents=FixtureDocumentsClient(store),
        config=FixtureConfigClient(store),
        mcp=FixtureDynatraceMcp(store),
        mode="synth",
    )
    snap = pipeline.run(conn, settings, w.at, RecordedResolver(answers))
    agent.mkdir(parents=True, exist_ok=True)
    note = "Recorded agent output for fixture mode (phase 03 produces it live with Gemini)."
    (agent / "usage_resolutions.json").write_text(
        json.dumps({"_about": note, "answers": answers}, indent=1, ensure_ascii=False) + "\n"
    )
    (agent / "findings.json").write_text(
        json.dumps(
            {"_about": note, "findings": write_findings(w, snap)}, indent=1, ensure_ascii=False
        )
        + "\n"
    )
    return snap


if __name__ == "__main__":
    s = main()
    print(
        f"{len(s.signals)} signals, {len(s.references)} references, {len(s.unresolved)} unresolved, "
        f"{s.scanned_bytes / GIB:.1f} GiB scanned in {len(s.queries)} queries"
    )
