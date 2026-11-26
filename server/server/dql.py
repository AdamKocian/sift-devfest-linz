"""Every DQL query Sift runs, in one place (documented in docs/DQL.md).

Queries are built by functions so the text is deterministic: the fixture client replays a
recording only when the stored text matches exactly.

DQL reference used: https://docs.dynatrace.com/docs/platform/grail/dynatrace-query-language
(fetch from:/samplingRatio:/scanLimitGBytes:, summarize ... by:{}, count, countIf, sum,
countDistinctExact, stringLength, matchesPhrase, coalesce, bin; `metrics` command).
Sampled aggregates are multiplied by dt.system.sampling_ratio as the fetch docs advise."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from server.connectors.grail import Query

UNKNOWN = "unknown"
SAMPLING_RATIOS = (1, 10, 100, 1000, 10000, 100000)  # valid fetch samplingRatio values


@dataclass(frozen=True)
class DiscoverParams:
    """Knobs of the discovery queries (settings key `discover`)."""

    sampling_ratio: int = 1
    span_attributes: tuple[str, ...] = ("db.statement",)
    sample_lines: int = 1000
    first_seen_days: int = 90

    @classmethod
    def from_settings(cls, d: dict | None) -> DiscoverParams:
        d = d or {}
        ratio = int(d.get("sampling_ratio", 1))
        if ratio not in SAMPLING_RATIOS:
            raise ValueError(f"sampling_ratio must be one of {SAMPLING_RATIOS}")
        return cls(
            sampling_ratio=ratio,
            span_attributes=tuple(d.get("span_attributes", cls.span_attributes)),
            sample_lines=int(d.get("sample_lines", cls.sample_lines)),
            first_seen_days=int(d.get("first_seen_days", cls.first_seen_days)),
        )


def lit(value: str) -> str:
    """A DQL string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def column(name: str) -> str:
    """'db.statement' -> 'db_statement' (result column for a measured attribute)."""
    return "".join(c if c.isalnum() else "_" for c in name)


def _sample(ratio: int) -> str:
    return f", samplingRatio: {ratio}" if ratio > 1 else ""


def _x(ratio: int) -> str:
    return " * takeAny(dt.system.sampling_ratio)" if ratio > 1 else ""


def _eq_or_missing(field: str, value: str) -> str:
    return f"isNull({field})" if value == UNKNOWN else f"{field} == {lit(value)}"


LOG_KEYS = (
    'service.name = coalesce(service.name, "unknown"), '
    'log.source = coalesce(log.source, "unknown"), '
    'loglevel = coalesce(loglevel, "NONE")'
)
SPAN_KEYS = 'service.name = coalesce(service.name, "unknown"), span.name'


# --- discover ---------------------------------------------------------------------------


def log_volume(p: DiscoverParams) -> Query:
    """Records and content size per log source per day, 15 days (14 full + today)."""
    r = p.sampling_ratio
    return Query(
        "log_volume",
        f"fetch logs, from: -15d{_sample(r)}\n"
        f"| summarize records = count(){_x(r)}, "
        f"content_chars = sum(stringLength(content)){_x(r)},\n"
        f"    by:{{{LOG_KEYS}, day = bin(timestamp, 1d)}}",
        max_records=50_000,
    )


def log_first_seen(p: DiscoverParams) -> Query:
    return Query(
        "log_first_seen",
        f"fetch logs, from: -{p.first_seen_days}d, samplingRatio: 1000\n"
        f"| summarize first_seen = min(timestamp), by:{{{LOG_KEYS}}}",
        max_records=50_000,
    )


def span_volume(p: DiscoverParams) -> Query:
    """Span count per service and span name per day, plus the size of large attributes."""
    r = p.sampling_ratio
    attrs = "".join(
        f',\n    attr_{column(a)} = sum(stringLength(coalesce({a}, ""))){_x(r)}'
        for a in p.span_attributes
    )
    return Query(
        "span_volume",
        f"fetch spans, from: -15d{_sample(r)}\n"
        f"| summarize records = count(){_x(r)}{attrs},\n"
        f"    by:{{{SPAN_KEYS}, day = bin(start_time, 1d)}}",
        max_records=50_000,
    )


def span_first_seen(p: DiscoverParams) -> Query:
    return Query(
        "span_first_seen",
        f"fetch spans, from: -{p.first_seen_days}d, samplingRatio: 1000\n"
        f"| summarize first_seen = min(start_time), by:{{{SPAN_KEYS}}}",
        max_records=50_000,
    )


def metric_series() -> Query:
    """Number of metric series per metric key and service.
    TODO(verify): `metrics` accepts from: and exposes dimensions as fields
    (https://docs.dynatrace.com/docs/platform/grail/dynatrace-query-language/commands/metric-commands)."""
    return Query(
        "metric_series",
        "metrics, from: -7d\n"
        "| summarize series = count(),\n"
        '    by:{metric.key, service.name = coalesce(service.name, "unknown")}',
        max_records=50_000,
    )


def metric_dimensions(key: str) -> Query:
    """One series of a metric: its fields are the dimension keys."""
    return Query(
        f"metric_dimensions:{key}",
        f"metrics, from: -7d\n| filter metric.key == {lit(key)}\n| limit 1",
        max_records=1,
    )


def metric_cardinality(key: str, dims: Sequence[str]) -> Query:
    """Series of a metric, and how many would remain without each dimension."""
    parts = []
    for d in dims:
        others = [o for o in dims if o != d]
        expr = ', "|", '.join(f'coalesce(toString({o}), "")' for o in others) or '""'
        parts.append(f"without_{column(d)} = countDistinctExact(concat({expr}))")
    cols = ",\n    ".join(["series = count()", *parts])
    return Query(
        f"metric_cardinality:{key}",
        f"metrics, from: -7d\n| filter metric.key == {lit(key)}\n| summarize {cols}",
        max_records=1,
    )


BILLING_INGEST_TYPES = (
    "Log Management & Analytics - Ingest & Process",
    "Traces - Ingest & Process",
    "Events - Ingest & Process",
)


def billing_ingest() -> Query:
    """Billed ingest bytes per capability per day. `dedup event.id` is mandatory:
    https://docs.dynatrace.com/docs/manage-your-costs/control/investigate-a-spike"""
    types = ", ".join(lit(t) for t in BILLING_INGEST_TYPES)
    return Query(
        "billing_ingest",
        "fetch dt.system.events, from: -15d\n"
        '| filter event.kind == "BILLING_USAGE_EVENT"\n'
        f"| filter in(event.type, {{{types}}})\n"
        "| dedup event.id\n"
        "| summarize billed_bytes = sum(billed_bytes), by:{event.type, day = bin(timestamp, 1d)}",
    )


# --- patterns ---------------------------------------------------------------------------


def log_source_filter(service: str, source: str, level: str) -> str:
    return " and ".join(
        [
            _eq_or_missing("service.name", service),
            _eq_or_missing("log.source", source),
            # Dynatrace sets loglevel to "NONE" when a record has no severity
            # (https://docs.dynatrace.com/docs/ingest-from/opentelemetry/otlp-api/ingest-logs).
            '(isNull(loglevel) or loglevel == "NONE")'
            if level == "NONE"
            else f"loglevel == {lit(level)}",
        ]
    )


def sample_ratio(records_day: float, wanted: int) -> int:
    """The largest valid samplingRatio that still returns about `wanted` lines from 24 h."""
    best = 1
    for r in SAMPLING_RATIOS:
        if records_day / r >= wanted:
            best = r
    return best


def log_sample(sid: str, service: str, source: str, level: str, ratio: int, n: int) -> Query:
    return Query(
        f"log_sample:{sid}",
        f"fetch logs, from: -24h{_sample(ratio)}\n"
        f"| filter {log_source_filter(service, source, level)}\n"
        f"| fields content\n| limit {n}",
        max_records=n,
    )


def pattern_volume(
    sid: str, service: str, source: str, level: str, phrases: Sequence[tuple[str, str]], ratio: int
) -> Query:
    """Exact volume of each mined pattern over 7 days. `phrases` = [(pattern id, phrase)]."""
    expr = lit("other")
    for pid, phrase in reversed(phrases):
        expr = f"if(matchesPhrase(content, {lit(phrase)}), {lit(pid)}, else: {expr})"
    return Query(
        f"pattern_volume:{sid}",
        f"fetch logs, from: -7d{_sample(ratio)}\n"
        f"| filter {log_source_filter(service, source, level)}\n"
        f"| fieldsAdd sift.pattern = {expr}\n"
        f"| summarize records = count(){_x(ratio)}, "
        f"content_chars = sum(stringLength(content)){_x(ratio)}, by:{{sift.pattern}}",
    )


# --- usage ------------------------------------------------------------------------------


def query_executions() -> Query:
    """Who ran which DQL in the last 90 days.
    TODO(verify): dt.system.query_executions has no official schema page; the field names
    (query_string, client.application_context, client.source, user.email) come from the
    community thread linked in docs/DQL.md. Documented alternatives with less detail:
    QUERY_EXECUTION_EVENT and the client.source of "... - Query" billing events."""
    return Query(
        "query_executions",
        "fetch dt.system.query_executions, from: -90d\n"
        "| summarize executions_30d = countIf(timestamp >= now() - 30d), "
        "last_run = max(timestamp),\n"
        "    by:{query_string, client.application_context, client.source, user.email}",
        max_records=50_000,
    )
