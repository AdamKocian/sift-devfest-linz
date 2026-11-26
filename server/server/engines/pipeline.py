"""Connectors -> Discover -> Patterns -> Usage. One function, used by fixture seeding, the
live discovery CLI and `make fixtures`, so every mode runs the same code."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from server import dql
from server.connectors import ConnectorError, Connectors
from server.dql import DiscoverParams
from server.engines import discover, ids, patterns, usage
from server.engines.discover import GIB, SignalDraft, Sizing
from server.engines.usage import Catalog, QueryResolver, RefDraft, Site, Unresolved


@dataclass
class Snapshot:
    signals: list[SignalDraft]
    references: list[RefDraft]
    unattributed_gib_day: dict[str, float]
    calibration: dict[str, float | None]
    unresolved: list[Unresolved]
    queries: list[dict[str, Any]]
    scanned_bytes: int
    mcp_tools: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    templates: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PatternParams:
    min_gib_day: float = 0.05
    min_share: float = 0.6

    @classmethod
    def from_settings(cls, d: dict[str, Any] | None) -> PatternParams:
        d = d or {}
        return cls(
            min_gib_day=float(d.get("pattern_min_gib_day", cls.min_gib_day)),
            min_share=float(d.get("pattern_min_share", cls.min_share)),
        )


def _patterns(
    conn: Connectors,
    src: SignalDraft,
    p: DiscoverParams,
    pp: PatternParams,
    sizing: Sizing,
    samples: dict[str, list[str]],
    templates: dict[str, str],
) -> list[SignalDraft]:
    assert src.source is not None and src.level is not None
    ratio = dql.sample_ratio(src.records_day, p.sample_lines)
    q = dql.log_sample(src.id, src.service, src.source, src.level, ratio, p.sample_lines)
    lines = [str(r["content"]) for r in conn.grail.query(q).records if r.get("content")]
    samples[src.id] = lines
    mined = patterns.mine(lines)
    src.samples = [t.examples[0] for t in mined[:3] if t.examples]
    chosen = []
    for t in patterns.dominant(mined, pp.min_share):
        words = patterns.phrase(t.template)
        if words:
            chosen.append((ids.log_pattern(src.id, patterns.display(t.template)), words, t))
    if not chosen:
        return []
    vq = dql.pattern_volume(
        src.id, src.service, src.source, src.level,
        [(pid, words) for pid, words, _ in chosen], p.sampling_ratio,
    )  # fmt: skip
    rows = {str(r.get("sift.pattern")): r for r in conn.grail.query(vq).records}
    oh = sizing.log_overhead_bytes

    def size(r: dict[str, Any] | None) -> tuple[float, float]:
        if r is None:
            return 0.0, 0.0
        n = discover.num(r.get("records"))
        return n, discover.num(r.get("content_chars")) + n * oh

    total_n = sum(size(r)[0] for r in rows.values())
    total_b = sum(size(r)[1] for r in rows.values())
    out = []
    for pid, _, t in chosen:
        n, b = size(rows.get(pid))
        if not total_b or not n:
            continue
        templates[pid] = patterns.display(t.template)
        out.append(
            SignalDraft(
                id=pid,
                kind="log_pattern",
                data_type="logs",
                service=src.service,
                key=patterns.display(t.template),
                parent_id=src.id,
                est_bytes_day=src.est_bytes_day * b / total_b,
                records_day=src.records_day * n / total_n,
                growth_wow=src.growth_wow,
                first_seen=src.first_seen,
                samples=t.examples,
            )
        )
    return out


def _dimensions(conn: Connectors, mets: list[SignalDraft], sizing: Sizing) -> list[SignalDraft]:
    out: list[SignalDraft] = []
    cache: dict[str, tuple[list[str], dict[str, Any]]] = {}
    per_day = sizing.metric_points_per_series_day
    for m in mets:
        if m.records_day / per_day < sizing.dimension_min_series:
            continue
        if m.key not in cache:
            dims = discover.dimension_keys(conn.grail.query(dql.metric_dimensions(m.key)).records)
            card: dict[str, Any] = {}
            if len(dims) >= 2:
                rows = conn.grail.query(dql.metric_cardinality(m.key, dims)).records
                card = rows[0] if rows else {}
            cache[m.key] = (dims, card)
        dims, card = cache[m.key]
        if card:
            out.extend(discover.metric_dimensions(m, card, dims, sizing))
    return out


def run(
    conn: Connectors, settings: dict[str, Any], now: datetime, resolver: QueryResolver
) -> Snapshot:
    p = DiscoverParams.from_settings(settings.get("discover"))
    sizing = Sizing.from_settings(settings.get("discover"))
    pp = PatternParams.from_settings(settings.get("discover"))
    g = conn.grail
    warnings: list[str] = []

    billing = g.query(dql.billing_ingest()).records
    logs = discover.log_sources(
        g.query(dql.log_volume(p)).records, g.query(dql.log_first_seen(p)).records, now, sizing
    )
    spans = discover.spans(
        g.query(dql.span_volume(p)).records,
        g.query(dql.span_first_seen(p)).records,
        now,
        sizing,
        p.span_attributes,
    )
    mets = discover.metrics(g.query(dql.metric_series()).records, now, sizing)

    samples: dict[str, list[str]] = {}
    templates: dict[str, str] = {}
    pats: list[SignalDraft] = []
    for src in logs:
        if src.est_bytes_day / GIB >= pp.min_gib_day:
            pats.extend(_patterns(conn, src, p, pp, sizing, samples, templates))
    dims = _dimensions(conn, mets, sizing)

    drafts = [*logs, *pats, *spans, *mets, *dims]
    factors, unattributed = discover.calibrate(drafts, discover.billed_per_day(billing, now))

    sites: list[Site] = []
    try:
        for doc in conn.documents.list():
            sites.extend(usage.document_sites(doc, conn.documents.content(doc.id)))
    except ConnectorError as exc:
        warnings.append(f"documents: {exc}")
    for name, fetch in (
        ("workflows", conn.config.workflows),
        ("alerts", conn.config.alerts),
        ("slos", conn.config.slos),
    ):
        try:
            sites.extend(usage.config_sites(fetch()))
        except ConnectorError as exc:
            warnings.append(f"{name}: {exc}")
    executions: list[dict[str, Any]] = []
    try:
        executions = g.query(dql.query_executions()).records
    except ConnectorError as exc:
        warnings.append(f"query executions: {exc}")
    sites.extend(usage.apply_activity(sites, executions, now))
    result = usage.usage(sites, Catalog(drafts, samples, templates), resolver)

    tools: list[str] = []
    try:
        tools = [t.name for t in conn.mcp.list_tools()]
    except ConnectorError as exc:
        warnings.append(f"mcp: {exc}")

    guard = g.guard
    return Snapshot(
        signals=drafts,
        references=result.references,
        unattributed_gib_day=unattributed,
        calibration=factors,
        unresolved=result.unresolved,
        queries=list(guard.log),
        scanned_bytes=guard.scanned_bytes,
        mcp_tools=tools,
        warnings=warnings,
        templates=templates,
    )
