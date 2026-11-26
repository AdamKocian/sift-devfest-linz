"""Grail DQL query API (execute + poll) with a cost guard.

API: https://docs.dynatrace.com/docs/dynatrace-api/platform-api/platform-storage-query-v1
- POST /platform/storage/query/v1/query:execute  -> 200 (finished) or 202 (poll with requestToken)
- GET  /platform/storage/query/v1/query:poll?request-token=&request-timeout-milliseconds=
- POST /platform/storage/query/v1/query:cancel
States: NOT_STARTED, RUNNING, SUCCEEDED, RESULT_GONE, CANCELLED, FAILED.
Result: result.records, result.types, result.metadata.grail.{scannedBytes, scannedRecords, ...}.
Scopes: one of storage:{logs,spans,metrics,events,system,buckets,...}:read for the tables read.

Grail bills queries by scanned bytes, so every query goes through `CostGuard`: a query must
name its timeframe (`from:` on every fetch/timeseries/metrics), stay inside the allowed
window and may not lift the scan limit; the scanned bytes of a run are added up and the
run stops when it crosses its budget."""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from server.connectors.base import (
    ConnectorError,
    DynatraceEnv,
    FixtureMismatchError,
    request,
)
from server.connectors.store import FixtureStore, format_ts, safe_name

QUERY_BASE = "/platform/storage/query/v1"
GIB = 1024**3


@dataclass(frozen=True)
class Query:
    """A named DQL query. The name keys the recorded fixture; the DQL is what runs."""

    name: str
    dql: str
    max_records: int = 10_000
    scan_limit_gb: float | None = None  # per-query override of the guard's default


@dataclass(frozen=True)
class QueryResult:
    records: list[dict[str, Any]]
    types: list[dict[str, Any]]
    scanned_bytes: int
    scanned_records: int
    execution_ms: int
    query_id: str | None
    sampled: bool
    notifications: list[dict[str, Any]]

    def field_type(self, index: int, name: str) -> str | None:
        """Type of a field in record `index` (Grail groups records into typed ranges)."""
        for t in self.types:
            lo, hi = (t.get("indexRange") or [0, len(self.records) - 1])[:2]
            if lo <= index <= hi:
                return (t.get("mappings", {}).get(name) or {}).get("type")
        return None


class GrailError(ConnectorError):
    pass


class CostLimitError(GrailError):
    pass


_FROM = re.compile(r"\bfrom\s*:\s*-\s*(\d+)\s*([dhm])\b")
_SOURCES = re.compile(r"(?:^|\|)\s*(fetch|timeseries|metrics)\b", re.MULTILINE)
_SCAN_LIMIT = re.compile(r"scanLimitGBytes\s*:\s*(-?\d+(?:\.\d+)?)")
_UNIT_DAYS = {"d": 1.0, "h": 1 / 24, "m": 1 / 1440}


@dataclass
class CostGuard:
    """Refuses queries that could scan more than we want to pay for."""

    max_timeframe_days: float = 90
    max_scan_gb_per_query: float = 50
    max_scan_gb_per_run: float = 200
    scanned_bytes: int = 0
    queries: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)

    def check(self, q: Query) -> None:
        sources = len(_SOURCES.findall(q.dql))
        frames = [int(n) * _UNIT_DAYS[u] for n, u in _FROM.findall(q.dql)]
        if sources and len(frames) < sources:
            raise CostLimitError(f"{q.name}: every fetch/timeseries/metrics needs `from:`")
        if frames and max(frames) > self.max_timeframe_days:
            raise CostLimitError(
                f"{q.name}: timeframe {max(frames):g} d exceeds {self.max_timeframe_days:g} d"
            )
        for limit in _SCAN_LIMIT.findall(q.dql):
            value = float(limit)
            if value < 0 or value > self.max_scan_gb_per_query:
                raise CostLimitError(f"{q.name}: scanLimitGBytes:{limit} is not allowed")
        if self.scanned_bytes >= self.max_scan_gb_per_run * GIB:
            raise CostLimitError(
                f"run budget of {self.max_scan_gb_per_run:g} GB scanned is used up"
            )

    def scan_limit(self, q: Query) -> float:
        return min(q.scan_limit_gb or self.max_scan_gb_per_query, self.max_scan_gb_per_query)

    def record(self, q: Query, result: QueryResult) -> None:
        self.queries += 1
        self.scanned_bytes += result.scanned_bytes
        self.log.append(
            {
                "name": q.name,
                "scanned_bytes": result.scanned_bytes,
                "records": len(result.records),
                "execution_ms": result.execution_ms,
            }
        )


def parse_response(body: dict[str, Any]) -> QueryResult:
    """QueryStartResponse / QueryPollResponse with state SUCCEEDED -> QueryResult."""
    state = body.get("state")
    if state != "SUCCEEDED":
        raise GrailError(f"query state {state}")
    result = body.get("result") or {}
    grail = (result.get("metadata") or {}).get("grail") or {}
    return QueryResult(
        records=list(result.get("records") or []),
        types=list(result.get("types") or []),
        scanned_bytes=int(grail.get("scannedBytes") or 0),
        scanned_records=int(grail.get("scannedRecords") or 0),
        execution_ms=int(grail.get("executionTimeMilliseconds") or 0),
        query_id=grail.get("queryId"),
        sampled=bool(grail.get("sampled")),
        notifications=list(grail.get("notifications") or []),
    )


class GrailClient(Protocol):
    guard: CostGuard

    def query(self, q: Query) -> QueryResult: ...


class LiveGrailClient:
    def __init__(
        self,
        env: DynatraceEnv,
        guard: CostGuard | None = None,
        *,
        client: httpx.Client | None = None,
        total_timeout_s: float = 120.0,
        poll_wait_ms: int = 5_000,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.env = env
        self.guard = guard or CostGuard()
        self.http = client or httpx.Client(
            base_url=env.platform_url, headers=env.headers(), timeout=env.timeout_s
        )
        self.total_timeout_s = total_timeout_s
        self.poll_wait_ms = poll_wait_ms
        self.clock = clock
        self.sleep = sleep
        self.last_body: dict[str, Any] | None = None  # for recording

    def query(self, q: Query) -> QueryResult:
        self.guard.check(q)
        body = {
            "query": q.dql,
            "timezone": "UTC",
            "locale": "en_US",
            "maxResultRecords": q.max_records,
            "defaultScanLimitGbytes": self.guard.scan_limit(q),
            "requestTimeoutMilliseconds": self.poll_wait_ms,
            "includeTypes": True,
        }
        resp = request(
            self.http,
            "POST",
            f"{QUERY_BASE}/query:execute",
            json=body,
            headers={"dt-client-context": "sift"},
            sleep=self.sleep,
        )
        data = resp.json()
        deadline = self.clock() + self.total_timeout_s
        token = data.get("requestToken")
        while data.get("state") in ("NOT_STARTED", "RUNNING"):
            if token is None:
                raise GrailError(f"{q.name}: query is {data.get('state')} but has no token")
            if self.clock() > deadline:
                self._cancel(token)
                raise GrailError(f"{q.name}: no result after {self.total_timeout_s:g} s")
            resp = request(
                self.http,
                "GET",
                f"{QUERY_BASE}/query:poll",
                params={
                    "request-token": token,
                    "request-timeout-milliseconds": self.poll_wait_ms,
                },
                sleep=self.sleep,
            )
            data = resp.json()
        if data.get("state") != "SUCCEEDED":
            raise GrailError(f"{q.name}: query ended in state {data.get('state')}")
        self.last_body = data
        result = parse_response(data)
        self.guard.record(q, result)
        return result

    def _cancel(self, token: str) -> None:
        # TODO(verify): whether query:cancel takes the token as query parameter or body.
        # https://docs.dynatrace.com/docs/dynatrace-api/platform-api/platform-storage-query-v1
        # Best effort; the query also stops when we stop polling.
        with contextlib.suppress(httpx.HTTPError):
            self.http.post(f"{QUERY_BASE}/query:cancel", params={"request-token": token})


class FixtureGrailClient:
    """Replays fixtures/dynatrace/grail/<name>.json. The stored DQL must match exactly, so a
    changed query cannot silently replay an old answer."""

    def __init__(self, store: FixtureStore, guard: CostGuard | None = None) -> None:
        self.store = store
        self.guard = guard or CostGuard()

    def query(self, q: Query) -> QueryResult:
        self.guard.check(q)
        data = self.store.read("grail", f"{safe_name(q.name)}.json")
        if data["query"].strip() != q.dql.strip():
            raise FixtureMismatchError(
                f"recorded DQL for {q.name} differs from the current query; "
                "re-record with `make fixtures` (tenant) or `make fixtures-synth`"
            )
        result = parse_response(data["response"])
        self.guard.record(q, result)
        return result


class RecordingGrailClient:
    """Runs live and writes each response (redacted) where the fixture client reads it."""

    def __init__(
        self,
        live: LiveGrailClient,
        store: FixtureStore,
        redact: Callable[[Any], Any],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.live = live
        self.guard = live.guard
        self.store = store
        self.redact = redact
        self.now = now

    def query(self, q: Query) -> QueryResult:
        result = self.live.query(q)
        assert self.live.last_body is not None
        self.store.write(
            {
                "name": q.name,
                "query": q.dql,
                "recorded_at": format_ts(self.now()),
                "response": self.redact(self.live.last_body),
            },
            "grail",
            f"{safe_name(q.name)}.json",
        )
        return result
