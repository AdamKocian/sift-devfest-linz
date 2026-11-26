"""Discover: signals with volume/day, records/day and size, from Grail query results.

Grail has no function that returns the stored size of a record, so the size of a signal is
estimated from what DQL can measure (content length, large attributes, series count) and
then calibrated: the estimates of a data type are scaled so they add up to the bytes
Dynatrace actually billed for that data type (BILLING_USAGE_EVENT, Ingest & Process).
Without billing data (or for metrics, which have no byte-based billing event) the estimate
stays uncalibrated and the signal says so."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from server.connectors.store import parse_ts
from server.dql import UNKNOWN, column
from server.engines import ids

GIB = 1024**3
WINDOW = 7  # days averaged for the current rate; the 7 before give week-over-week growth

BILLING_DATA_TYPE = {
    "Log Management & Analytics - Ingest & Process": "logs",
    "Traces - Ingest & Process": "spans",
    "Events - Ingest & Process": "events",
}


@dataclass(frozen=True)
class Sizing:
    """How record sizes are estimated (settings key `discover`)."""

    log_overhead_bytes: int = 180  # timestamp, level, resource attributes per log record
    span_base_bytes: int = 1200  # a span without its large attributes
    metric_points_per_series_day: int = 1440  # one data point per minute
    metric_bytes_per_point: int = 160
    attribute_min_share: float = 0.2  # a span attribute becomes a signal above this share
    dimension_min_series: int = 5000  # metrics checked for costly dimensions
    dimension_min_share: float = 0.5  # share of series a dimension must multiply

    @classmethod
    def from_settings(cls, d: dict[str, Any] | None) -> Sizing:
        d = d or {}
        known = cls.__dataclass_fields__
        return cls(**{k: type(getattr(cls(), k))(v) for k, v in d.items() if k in known})


@dataclass
class SignalDraft:
    id: str
    kind: str
    data_type: str
    service: str
    key: str
    est_bytes_day: float
    records_day: float
    first_seen: datetime
    parent_id: str | None = None
    source: str | None = None
    level: str | None = None
    growth_wow: float = 0.0
    gib_day: float = 0.0  # set by calibrate()
    calibrated: bool = False
    samples: list[str] = field(default_factory=list)

    @property
    def avg_record_bytes(self) -> int:
        if self.records_day <= 0:
            return 0
        return max(1, round(self.gib_day * GIB / self.records_day))


@dataclass
class DiscoverResult:
    signals: list[SignalDraft]
    unattributed_gib_day: dict[str, float]
    calibration: dict[str, float | None]  # data type -> billed / estimated, None = no billing
    billed_gib_day: dict[str, float]


def num(value: Any) -> float:
    """Grail returns longs as JSON numbers or strings; nulls count as 0."""
    if value is None:
        return 0.0
    return float(value)


# --- daily windows ----------------------------------------------------------------------


@dataclass
class Daily:
    by_day: dict[datetime, float] = field(default_factory=lambda: defaultdict(float))

    def add(self, day: datetime, value: float) -> None:
        self.by_day[day] += value


def _full_days(days: Iterable[datetime], now: datetime) -> list[datetime]:
    return sorted(d for d in set(days) if d + timedelta(days=1) <= now)


def window_stats(daily: Daily, all_days: list[datetime]) -> tuple[float, float]:
    """(average per day over the last 7 full days, week-over-week growth).

    `all_days` are the full-day bins of the whole result, so a signal that is silent on a
    day counts as 0 that day, and a signal that appeared 3 days ago is averaged over 3."""
    recent = all_days[-WINDOW:]
    previous = all_days[-2 * WINDOW : -WINDOW]
    seen = [d for d in all_days if daily.by_day.get(d, 0) > 0]
    if not recent or not seen:
        return 0.0, 0.0
    span = [d for d in recent if d >= seen[0]] or recent
    avg = sum(daily.by_day.get(d, 0.0) for d in recent) / len(span)
    prev = sum(daily.by_day.get(d, 0.0) for d in previous)
    growth = 0.0
    if len(previous) == WINDOW and prev > 0 and seen[0] <= previous[0]:
        growth = sum(daily.by_day.get(d, 0.0) for d in recent) / prev - 1
    return avg, growth


def _first_seen(
    rows: list[dict[str, Any]], keys: tuple[str, ...]
) -> dict[tuple[str, ...], datetime]:
    out = {}
    for r in rows:
        if r.get("first_seen"):
            out[tuple(str(r.get(k) or UNKNOWN) for k in keys)] = parse_ts(r["first_seen"])
    return out


# --- logs -------------------------------------------------------------------------------

LOG_KEYS = ("service.name", "log.source", "loglevel")


def log_sources(
    volume: list[dict[str, Any]],
    first_seen: list[dict[str, Any]],
    now: datetime,
    sizing: Sizing,
) -> list[SignalDraft]:
    days = _full_days((parse_ts(r["day"]) for r in volume), now)
    records: dict[tuple[str, ...], Daily] = defaultdict(Daily)
    size: dict[tuple[str, ...], Daily] = defaultdict(Daily)
    for r in volume:
        key = tuple(str(r.get(k) or UNKNOWN) for k in LOG_KEYS)
        day = parse_ts(r["day"])
        n = num(r.get("records"))
        records[key].add(day, n)
        size[key].add(day, num(r.get("content_chars")) + n * sizing.log_overhead_bytes)
    seen = _first_seen(first_seen, LOG_KEYS)
    out = []
    for key in sorted(records):
        service, source, level = key
        rec, _ = window_stats(records[key], days)
        est, growth = window_stats(size[key], days)
        if rec <= 0:
            continue
        out.append(
            SignalDraft(
                id=ids.log_source(service, source, level),
                kind="log_source",
                data_type="logs",
                service=service,
                key=f"{source} · {level}",
                source=source,
                level=level,
                est_bytes_day=est,
                records_day=rec,
                growth_wow=growth,
                first_seen=seen.get(key) or min(records[key].by_day),
            )
        )
    return out


# --- spans ------------------------------------------------------------------------------

SPAN_KEYS = ("service.name", "span.name")


def spans(
    volume: list[dict[str, Any]],
    first_seen: list[dict[str, Any]],
    now: datetime,
    sizing: Sizing,
    attributes: Iterable[str],
) -> list[SignalDraft]:
    attributes = tuple(attributes)
    days = _full_days((parse_ts(r["day"]) for r in volume), now)
    records: dict[tuple[str, ...], Daily] = defaultdict(Daily)
    size: dict[tuple[str, ...], Daily] = defaultdict(Daily)
    attr: dict[tuple[str, ...], dict[str, Daily]] = defaultdict(lambda: defaultdict(Daily))
    for r in volume:
        key = tuple(str(r.get(k) or UNKNOWN) for k in SPAN_KEYS)
        day = parse_ts(r["day"])
        n = num(r.get("records"))
        extra = 0.0
        for a in attributes:
            chars = num(r.get(f"attr_{column(a)}"))
            attr[key][a].add(day, chars)
            extra += chars
        records[key].add(day, n)
        size[key].add(day, n * sizing.span_base_bytes + extra)
    seen = _first_seen(first_seen, SPAN_KEYS)
    out = []
    for key in sorted(records):
        service, name = key
        rec, _ = window_stats(records[key], days)
        est, growth = window_stats(size[key], days)
        if rec <= 0:
            continue
        parent = SignalDraft(
            id=ids.span(service, name),
            kind="span",
            data_type="spans",
            service=service,
            key=name,
            est_bytes_day=est,
            records_day=rec,
            growth_wow=growth,
            first_seen=seen.get(key) or min(records[key].by_day),
        )
        out.append(parent)
        for a in attributes:
            a_bytes, _ = window_stats(attr[key][a], days)
            if est > 0 and a_bytes / est >= sizing.attribute_min_share:
                out.append(
                    SignalDraft(
                        id=ids.span_attribute(parent.id, a),
                        kind="span_attribute",
                        data_type="spans",
                        service=service,
                        key=a,
                        parent_id=parent.id,
                        est_bytes_day=a_bytes,
                        records_day=rec,
                        growth_wow=growth,
                        first_seen=parent.first_seen,
                    )
                )
    return out


# --- metrics ----------------------------------------------------------------------------


def metrics(series: list[dict[str, Any]], now: datetime, sizing: Sizing) -> list[SignalDraft]:
    """One signal per metric key and service. Data points/day = series × points per series."""
    out = []
    per_day = sizing.metric_points_per_series_day
    for r in sorted(series, key=lambda r: (str(r.get("service.name")), str(r["metric.key"]))):
        service = str(r.get("service.name") or UNKNOWN)
        key = str(r["metric.key"])
        n = num(r.get("series"))
        if n <= 0:
            continue
        out.append(
            SignalDraft(
                id=ids.metric(service, key),
                kind="metric",
                data_type="metrics",
                service=service,
                key=key,
                est_bytes_day=n * per_day * sizing.metric_bytes_per_point,
                records_day=n * per_day,
                first_seen=now - timedelta(days=WINDOW),  # `metrics` sees the last 10 days only
            )
        )
    return out


NON_DIMENSIONS = {"metric.key", "timeframe", "interval", "dt.system.bucket"}


def dimension_keys(sample: list[dict[str, Any]]) -> list[str]:
    """Dimension keys of a metric from one of its series."""
    if not sample:
        return []
    return sorted(k for k in sample[0] if k not in NON_DIMENSIONS and not k.startswith("dt.raw"))


def metric_dimensions(
    metric: SignalDraft, cardinality: dict[str, Any], dims: list[str], sizing: Sizing
) -> list[SignalDraft]:
    """A dimension becomes a signal when dropping it would remove most of the series."""
    total = num(cardinality.get("series"))
    out = []
    if total <= 0:
        return out
    for d in dims:
        without = num(cardinality.get(f"without_{column(d)}"))
        share = 1 - without / total if without else 0.0
        if share >= sizing.dimension_min_share:
            out.append(
                SignalDraft(
                    id=ids.metric_dimension(metric.id, d),
                    kind="metric_dimension",
                    data_type="metrics",
                    service=metric.service,
                    key=d,
                    parent_id=metric.id,
                    est_bytes_day=metric.est_bytes_day * share,
                    records_day=metric.records_day * share,
                    first_seen=metric.first_seen,
                )
            )
    return out


# --- calibration ------------------------------------------------------------------------


def billed_per_day(billing: list[dict[str, Any]], now: datetime) -> dict[str, float]:
    """Billed bytes per day and data type over the same 7 full days as the volumes."""
    days = _full_days((parse_ts(r["day"]) for r in billing), now)
    by_type: dict[str, Daily] = defaultdict(Daily)
    for r in billing:
        dt = BILLING_DATA_TYPE.get(str(r.get("event.type")))
        if dt:
            by_type[dt].add(parse_ts(r["day"]), num(r.get("billed_bytes")))
    return {dt: window_stats(d, days)[0] for dt, d in by_type.items()}


def calibrate(
    drafts: list[SignalDraft], billed_bytes_day: dict[str, float]
) -> tuple[dict[str, float | None], dict[str, float]]:
    """Scale estimates so each data type adds up to its billed bytes. Children (patterns,
    attributes, dimensions) are subsets of their parents and are scaled the same way.
    Returns (factor per data type, unattributed GiB/day per data type without signals)."""
    top = defaultdict(float)
    for d in drafts:
        if d.parent_id is None:
            top[d.data_type] += d.est_bytes_day
    factors: dict[str, float | None] = {}
    for dt in {d.data_type for d in drafts}:
        billed = billed_bytes_day.get(dt)
        factors[dt] = billed / top[dt] if billed and top[dt] else None
    for d in drafts:
        f = factors.get(d.data_type)
        d.calibrated = f is not None
        d.gib_day = d.est_bytes_day * (f or 1.0) / GIB
    unattributed = {
        dt: b / GIB for dt, b in billed_bytes_day.items() if dt not in factors and b > 0
    }
    return factors, unattributed
