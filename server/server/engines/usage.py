"""Usage: which signals each dashboard, notebook, workflow, alert, SLO and ad-hoc query reads.

Inputs are plain connector data: document contents, config query sites and query
execution rows. Each query is read by the deterministic parser; what the parser cannot
resolve goes to a `QueryResolver` (Gemini in phase 03, recorded answers in fixture mode,
a mock in tests). Attribution is conservative: when a filter cannot be mapped precisely,
the query counts as reading every signal it might read, because a missed reader can break
a dashboard while an extra one only keeps data that could have been dropped."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from server.connectors.dtconfig import QuerySite
from server.connectors.store import parse_ts
from server.engines import patterns
from server.engines.discover import SignalDraft
from server.engines.dql_parser import (
    ParsedQuery,
    Term,
    Variable,
    parse,
    phrases_for,
    substitute,
)

# Fields Sift can map to signals. A filter on another field (a host, a pod) does not narrow
# the attribution, and a query restricted only by such fields is left for Gemini.
LOG_FIELDS = {"service.name", "loglevel", "log.source", "status"}
SPAN_FIELDS = {"service.name", "span.name"}
METRIC_FIELDS = {"service.name"}
KNOWN_FIELDS = LOG_FIELDS | SPAN_FIELDS | {"content"}

APP_LABEL = {
    "dynatrace.notebooks": "Notebooks",
    "dynatrace.logs": "Logs",
    "dynatrace.distributedtracing": "Distributed Tracing",
    "dynatrace.dashboards": "Dashboards",
}


@dataclass
class Site:
    """One query that something runs: a dashboard tile, a notebook section, a workflow
    task, an alert, an SLO, or an ad-hoc query someone typed."""

    type: str  # dashboard | notebook | workflow | alert | slo | query
    id: str
    name: str
    owner: str
    dql: str
    part: str | None = None
    variables: dict[str, Variable] = field(default_factory=dict)
    every_minutes: float | None = None
    count_30d: int = 0
    last_used: datetime | None = None


@dataclass
class RefDraft:
    signal_id: str
    type: str
    name: str
    owner: str
    count_30d: int
    last_used: datetime | None
    fields: list[str]
    dql: str | None
    note: str | None = None
    resolved_by: str = "parser"  # parser | gemini


@dataclass(frozen=True)
class Resolution:
    """What a resolver says an unresolved query reads."""

    signal_ids: tuple[str, ...]
    fields: tuple[str, ...]
    note: str
    model: str


class QueryResolver(Protocol):
    def resolve(self, site: Site, reason: str, catalog: list[SignalDraft]) -> Resolution | None: ...


class NoResolver:
    """Leaves unresolved queries unresolved (live mode until phase 03)."""

    def resolve(self, site: Site, reason: str, catalog: list[SignalDraft]) -> Resolution | None:
        return None


def query_key(dql: str) -> str:
    """Whitespace-insensitive identity of a query text."""
    return hashlib.sha1(" ".join(dql.split()).encode()).hexdigest()[:16]


class RecordedResolver:
    """Replays recorded Gemini answers keyed by query_key (fixtures/dynatrace/gemini/)."""

    def __init__(self, answers: Mapping[str, dict[str, Any]]) -> None:
        self.answers = answers

    def resolve(self, site: Site, reason: str, catalog: list[SignalDraft]) -> Resolution | None:
        a = self.answers.get(query_key(site.dql))
        if a is None:
            return None
        known = {s.id for s in catalog}
        return Resolution(
            signal_ids=tuple(s for s in a["signal_ids"] if s in known),
            fields=tuple(a.get("fields", [])),
            note=a["note"],
            model=a["model"],
        )


@dataclass
class Unresolved:
    site: Site
    reason: str


@dataclass
class UsageResult:
    references: list[RefDraft]
    unresolved: list[Unresolved]
    sites: int


# --- sites from documents and config ----------------------------------------------------


def dashboard_variables(content: dict[str, Any]) -> dict[str, Variable]:
    """Variables with a static value. Query variables have none: their values come from
    running a DQL query, so a tile that uses one is unresolved for the parser."""
    out = {}
    for v in content.get("variables") or []:
        key, vtype = v.get("key"), v.get("type")
        if not key or vtype == "query":
            continue
        multiple = bool(v.get("multiple"))
        if vtype == "csv":
            # A viewer can pick any option, so the tile may read data for each of them.
            values = [o.strip() for o in str(v.get("input") or "").split(",") if o.strip()]
            multiple = True
        else:
            default = v.get("defaultValue")
            values = [str(default)] if default not in (None, "") else []
        out[key] = Variable(key, tuple(str(x) for x in values), multiple)
    return out


def document_sites(doc: Any, content: dict[str, Any]) -> list[Site]:
    """Dashboard data tiles and notebook DQL sections."""
    sites = []
    if doc.type == "dashboard":
        variables = dashboard_variables(content)
        for tid, tile in sorted((content.get("tiles") or {}).items()):
            if tile.get("type") == "data" and tile.get("query"):
                sites.append(
                    Site("dashboard", doc.id, doc.name, doc.owner, tile["query"],
                         part=tile.get("title") or f"tile {tid}", variables=variables)
                )  # fmt: skip
    elif doc.type == "notebook":
        for i, sec in enumerate(content.get("sections") or []):
            value = ((sec.get("state") or {}).get("input") or {}).get("value")
            if sec.get("type") == "dql" and value:
                sites.append(
                    Site("notebook", doc.id, doc.name, doc.owner, value,
                         part=sec.get("title") or f"section {i + 1}")
                )  # fmt: skip
    return sites


def config_sites(items: Iterable[QuerySite]) -> list[Site]:
    return [
        Site(q.type, q.id, q.name, q.owner, q.dql, part=q.part, every_minutes=q.every_minutes)
        for q in items
    ]


# --- activity ---------------------------------------------------------------------------


def _norm(dql: str) -> str:
    return " ".join(dql.split())


def apply_activity(
    sites: list[Site], executions: list[dict[str, Any]], now: datetime
) -> list[Site]:
    """Fill count_30d / last_used of each site from query execution rows and return the
    ad-hoc sites built from executions that belong to no known site.

    A row belongs to a site when its client.source names the site's id (a dashboard URL
    contains the document id, an alert its objectId, a workflow its id)."""
    by_id: dict[str, list[Site]] = defaultdict(list)
    for s in sites:
        by_id[s.id].append(s)
    rows_for: dict[str, list[dict[str, Any]]] = defaultdict(list)
    adhoc: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    ids = sorted(by_id, key=len, reverse=True)
    for r in executions:
        src = str(r.get("client.source") or "")
        owner = next((i for i in ids if i and i in src), None)
        if owner is not None:
            rows_for[owner].append(r)
        elif r.get("query_string"):
            app = str(r.get("client.application_context") or "API")
            adhoc[(app, _norm(str(r["query_string"])))].append(r)

    for sid, group in by_id.items():
        rows = rows_for.get(sid, [])
        matched: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            text = _norm(str(r.get("query_string") or ""))
            for i, s in enumerate(group):
                if _norm(s.dql) == text or _norm(_resolved_text(s)) == text:
                    matched[i].append(r)
        site_max = max(
            (sum(int(x.get("executions_30d") or 0) for x in m) for m in matched.values()), default=0
        )
        site_total = sum(int(x.get("executions_30d") or 0) for x in rows)
        site_last = max((parse_ts(x["last_run"]) for x in rows if x.get("last_run")), default=None)
        for i, s in enumerate(group):
            mine = matched.get(i)
            if mine:
                s.count_30d = sum(int(x.get("executions_30d") or 0) for x in mine)
                s.last_used = max(parse_ts(x["last_run"]) for x in mine if x.get("last_run"))
            elif rows:
                s.count_30d = site_max or site_total
                s.last_used = site_last
            elif s.every_minutes:
                # Configured to run on a schedule, but no execution rows (e.g. no access):
                # trust the config.
                s.count_30d = int(30 * 24 * 60 / s.every_minutes)
                s.last_used = now - timedelta(minutes=s.every_minutes)

    out = []
    for (app, text), rows in sorted(adhoc.items()):
        owners = sorted({str(r.get("user.email") or "") for r in rows} - {""})
        out.append(
            Site(
                "query",
                f"adhoc-{query_key(text)}",
                f"Ad-hoc query · {APP_LABEL.get(app, app)}",
                ", ".join(owners) or "unknown",
                str(rows[0]["query_string"]),
                count_30d=sum(int(r.get("executions_30d") or 0) for r in rows),
                last_used=max(
                    (parse_ts(r["last_run"]) for r in rows if r.get("last_run")), default=None
                ),
            )
        )
    return out


def _resolved_text(s: Site) -> str:
    return substitute(s.dql, s.variables)[0]


# --- attribution ------------------------------------------------------------------------


@dataclass
class Catalog:
    """Signals indexed for attribution, plus raw sample lines per log source."""

    signals: list[SignalDraft]
    samples: dict[str, list[str]] = field(default_factory=dict)  # log source id -> raw lines
    templates: dict[str, str] = field(default_factory=dict)  # pattern id -> template

    def by_kind(self, *kinds: str) -> list[SignalDraft]:
        return [s for s in self.signals if s.kind in kinds]

    def children(self, sid: str) -> list[SignalDraft]:
        return [s for s in self.signals if s.parent_id == sid]


STATUS_LEVELS = {"ERROR": {"ERROR", "FATAL", "CRITICAL", "SEVERE", "EMERGENCY", "ALERT"}}


def _level_ok(term: Term, level: str | None) -> bool:
    if level is None:
        return True
    if not term.allows("loglevel", level, case_insensitive=True):
        return False
    status = term.eq.get("status")
    if status and {v.upper() for v in status} <= set(STATUS_LEVELS):
        # status is derived from loglevel; only the ERROR mapping is certain enough to narrow
        allowed = set().union(*(STATUS_LEVELS[v.upper()] for v in status))
        return level.upper() in allowed
    return True


def _restricted_only_by_unknown(q: ParsedQuery, known: set[str]) -> bool:
    for t in q.terms:
        if any(f in known for f in t.eq) or t.phrases:
            return False
    return any(f not in known for t in q.terms for f in t.eq)


def attribute(q: ParsedQuery, cat: Catalog) -> list[tuple[str, str | None]]:
    """[(signal id, note)] the parsed query reads."""
    out: list[tuple[str, str | None]] = []
    if q.source == "logs":
        for t in q.terms:
            scope = [
                s for s in cat.by_kind("log_source")
                if t.allows("service.name", s.service)
                and t.allows("log.source", s.source)
                and _level_ok(t, s.level)
            ]  # fmt: skip
            words = phrases_for(t)
            if not words:
                out.extend((s.id, None) for s in scope)
                continue
            hits: list[tuple[str, str | None]] = []
            sampled = [s for s in scope if s.id in cat.samples]
            hits.extend((s.id, None) for s in scope if s.id not in cat.samples)  # cannot check
            for s in sampled:
                pats = [
                    p for p in cat.children(s.id)
                    if all(patterns.matches(w, cat.templates.get(p.id, p.key)) for w in words)
                ]  # fmt: skip
                if pats:
                    hits.extend((p.id, None) for p in pats)
                elif any(
                    all(patterns.matches(w, line) for w in words)
                    for line in cat.samples.get(s.id, [])
                ):
                    hits.append((s.id, None))
            if not hits:
                note = "Phrase not seen in sampled lines; counted for every source it could match."
                hits = [(s.id, note) for s in sampled]
            out.extend(hits)
    elif q.source == "spans":
        for t in q.terms:
            out.extend(
                (s.id, None)
                for s in cat.by_kind("span")
                if t.allows("service.name", s.service) and t.allows("span.name", s.key)
            )
    elif q.source == "metrics":
        for key in q.metric_keys:
            for t in q.terms:
                out.extend(
                    (s.id, None)
                    for s in cat.by_kind("metric")
                    if s.key == key and t.allows("service.name", s.service)
                )
    seen: dict[str, str | None] = {}
    for sid, note in out:
        seen.setdefault(sid, note)
    return list(seen.items())


def _every_note(site: Site) -> str | None:
    if not site.every_minutes:
        return None
    m = site.every_minutes
    if site.type == "workflow":
        unit = "hour" if m == 60 else f"{m:g} minutes" if m < 60 else f"{m / 60:g} hours"
        return f"Runs every {unit}."
    unit = "minute" if m == 1 else f"{m:g} minutes"
    return f"Evaluated every {unit}."


def usage(sites: list[Site], cat: Catalog, resolver: QueryResolver) -> UsageResult:
    refs: dict[tuple[str, str, str], RefDraft] = {}
    unresolved: list[Unresolved] = []

    def add(site: Site, sid: str, fields: Iterable[str], note: str | None, by: str) -> None:
        key = (sid, site.type, site.id)
        r = refs.get(key)
        if r is None:
            refs[key] = RefDraft(
                signal_id=sid,
                type=site.type,
                name=site.name,
                owner=site.owner,
                count_30d=site.count_30d,
                last_used=site.last_used,
                fields=sorted(set(fields)),
                dql=site.dql,
                note=note or _every_note(site),
                resolved_by=by,
            )
            return
        r.fields = sorted(set(r.fields) | set(fields))
        r.count_30d = max(r.count_30d, site.count_30d)
        if site.last_used and (r.last_used is None or site.last_used > r.last_used):
            r.last_used = site.last_used
        if note and not r.note:
            r.note = note
        if by == "gemini":
            r.resolved_by = by

    for site in sites:
        q = parse(site.dql, site.variables)
        reason = q.unresolved
        if reason is None and q.source in ("logs", "spans"):
            known = LOG_FIELDS if q.source == "logs" else SPAN_FIELDS
            if _restricted_only_by_unknown(q, known | {"content"}):
                fields = sorted({f for t in q.terms for f in t.eq} - known)
                reason = f"filters on {', '.join(fields)}, which Sift cannot map to signals"
        if reason is not None:
            res = resolver.resolve(site, reason, cat.signals)
            if res is None:
                unresolved.append(Unresolved(site, reason))
                continue
            for sid in res.signal_ids:
                add(site, sid, res.fields, res.note, "gemini")
            continue
        fields = set(q.fields)
        if not q.projected and q.source in ("logs", "spans"):
            fields.add("*")  # raw records: every attribute is shown
        for sid, note in attribute(q, cat):
            add(site, sid, fields, note, "parser")
    return UsageResult(
        references=sorted(refs.values(), key=lambda r: (r.signal_id, r.type, r.name)),
        unresolved=unresolved,
        sites=len(sites),
    )
