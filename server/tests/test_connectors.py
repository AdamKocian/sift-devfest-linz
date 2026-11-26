"""Connectors: live clients against mocked HTTP, the cost guard, and fixture replay."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from server.config import REPO_ROOT
from server.connectors import fixture_connectors
from server.connectors.base import ConnectorError, DynatraceEnv, FixtureMismatchError
from server.connectors.documents import LiveDocumentsClient
from server.connectors.dtconfig import LiveConfigClient
from server.connectors.grail import (
    GIB,
    CostGuard,
    CostLimitError,
    FixtureGrailClient,
    GrailError,
    LiveGrailClient,
    Query,
)
from server.connectors.store import FixtureStore, format_ts, parse_ts, shift

ENV = DynatraceEnv("https://abc12345.apps.dynatrace.com", "dt0s16.TEST")
OK = {
    "state": "SUCCEEDED",
    "progress": 100,
    "result": {
        "records": [{"n": "3"}],
        "types": [{"indexRange": [0, 0], "mappings": {"n": {"type": "long"}}}],
        "metadata": {"grail": {"scannedBytes": 2 * GIB, "scannedRecords": 9, "queryId": "q1"}},
    },
}
Q = Query("t", "fetch logs, from: -1d\n| summarize n = count()")


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(base_url=ENV.platform_url, transport=httpx.MockTransport(handler))


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def test_execute_then_poll_until_succeeded() -> None:
    calls: list[httpx.Request] = []
    states = iter(["RUNNING", "SUCCEEDED"])

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.path.endswith("query:execute"):
            body = json.loads(req.content)
            assert body["query"] == Q.dql and body["defaultScanLimitGbytes"] == 50
            return httpx.Response(202, json={"state": "RUNNING", "requestToken": "tok+1"})
        assert req.url.params["request-token"] == "tok+1"
        state = next(states)
        return httpx.Response(200, json=OK if state == "SUCCEEDED" else {"state": state})

    g = LiveGrailClient(ENV, client=_client(handler))
    r = g.query(Q)
    assert r.records == [{"n": "3"}] and r.scanned_bytes == 2 * GIB
    assert [c.url.path.rsplit("/", 1)[-1] for c in calls] == [
        "query:execute",
        "query:poll",
        "query:poll",
    ]
    assert g.guard.scanned_bytes == 2 * GIB and g.guard.queries == 1


def test_poll_timeout_cancels_the_query() -> None:
    clock = Clock()
    paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        paths.append(req.url.path.rsplit("/", 1)[-1])
        clock.t += 30
        if paths[-1] == "query:cancel":
            return httpx.Response(200, json={})
        return httpx.Response(202, json={"state": "RUNNING", "requestToken": "t"})

    g = LiveGrailClient(ENV, client=_client(handler), total_timeout_s=60, clock=clock)
    with pytest.raises(GrailError, match="no result after 60 s"):
        g.query(Q)
    assert paths[-1] == "query:cancel"


def test_failed_query_and_http_errors() -> None:
    g = LiveGrailClient(
        ENV, client=_client(lambda r: httpx.Response(200, json={"state": "FAILED"}))
    )
    with pytest.raises(GrailError, match="FAILED"):
        g.query(Q)
    clock = Clock()
    g = LiveGrailClient(
        ENV,
        client=_client(lambda r: httpx.Response(429, headers={"Retry-After": "2"})),
        sleep=clock.sleep,
    )
    with pytest.raises(ConnectorError, match="429 after 3 attempts"):
        g.query(Q)
    assert clock.t == 4  # two waits of Retry-After seconds
    g = LiveGrailClient(ENV, client=_client(lambda r: httpx.Response(403, text="no scope")))
    with pytest.raises(ConnectorError, match="403"):
        g.query(Q)


@pytest.mark.parametrize(
    ("dql", "msg"),
    [
        ("fetch logs | summarize count()", "needs `from:`"),
        ("fetch logs, from: -120d | limit 1", "exceeds 90 d"),
        ("fetch logs, from: -1d, scanLimitGBytes: -1 | limit 1", "not allowed"),
        ("fetch logs, from: -1d | lookup [fetch spans] | limit 1", "needs `from:`"),
    ],
)
def test_cost_guard_refuses_expensive_queries(dql: str, msg: str) -> None:
    with pytest.raises(CostLimitError, match=msg):
        CostGuard().check(Query("x", dql))


def test_cost_guard_stops_a_run_at_its_budget() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=OK)

    g = LiveGrailClient(ENV, CostGuard(max_scan_gb_per_run=3), client=_client(handler))
    g.query(Q)
    g.query(Q)  # 4 GiB scanned now
    with pytest.raises(CostLimitError, match="budget"):
        g.query(Q)


def test_fixture_replay_shifts_time_and_checks_the_query(tmp_path: Path) -> None:
    store = FixtureStore(tmp_path)
    store.write(
        {
            "name": "t",
            "query": Q.dql,
            "recorded_at": "2026-11-20T12:00:00.000000000Z",
            "response": OK
            | {"result": OK["result"] | {"records": [{"last": "2026-11-20T11:00:00.000000000Z"}]}},
        },
        "grail",
        "t.json",
    )
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    r = FixtureGrailClient(FixtureStore(tmp_path, now)).query(Q)
    assert parse_ts(r.records[0]["last"]) == now - timedelta(hours=1)
    with pytest.raises(FixtureMismatchError):
        FixtureGrailClient(FixtureStore(tmp_path, now)).query(Query("t", "fetch logs, from: -2d"))


def test_timestamp_shift_keeps_grail_format() -> None:
    out = shift({"a": ["2026-01-01T00:00:00Z", "x"]}, timedelta(days=1))
    assert out == {"a": ["2026-01-02T00:00:00.000000000Z", "x"]}
    assert format_ts(parse_ts("2026-01-01T00:00:00.123456789Z")).endswith(".123456000Z")


def test_documents_paging_and_multipart_content() -> None:
    boundary = "b0undary"
    content = {"version": 21, "tiles": {"1": {"type": "data", "query": "fetch logs"}}}
    multipart = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n'
        'Content-Type: application/json\r\n\r\n{"id": "d1"}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="content"; filename="c"\r\n'
        f"Content-Type: application/json\r\n\r\n{json.dumps(content)}\r\n--{boundary}--\r\n"
    ).encode()
    seen: list[dict[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/documents"):
            seen.append(dict(req.url.params))
            if "page-key" not in req.url.params:
                page = {"documents": [{"id": "d1", "name": "A", "type": "dashboard"}],
                        "nextPageKey": "k2"}  # fmt: skip
            else:
                mod = {"lastModifiedTime": "2026-11-01T00:00:00Z"}
                page = {"documents": [{"id": "n1", "name": "B", "type": "notebook",
                                       "modificationInfo": mod}]}  # fmt: skip
            return httpx.Response(200, json=page)
        return httpx.Response(
            200,
            content=multipart,
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )

    docs = LiveDocumentsClient(ENV, client=_client(handler))
    listed = docs.list()
    assert [d.id for d in listed] == ["d1", "n1"] and listed[1].last_modified is not None
    assert seen[0]["filter"] == "type in ('dashboard', 'notebook')" and seen[1] == {
        "page-size": "1000",
        "page-key": "k2",
    }
    assert docs.content("d1") == content


def test_workflow_tasks_with_a_query_are_query_sites() -> None:
    body = {
        "count": 1,
        "results": [
            {
                "id": "w1",
                "title": "Restock",
                "owner": "u",
                "trigger": {"schedule": {"trigger": {"type": "interval", "intervalMinutes": 60}}},
                "tasks": {
                    "q": {
                        "name": "q",
                        "action": "x:execute-dql-query",
                        "input": {"query": "fetch logs"},
                    },
                    "mail": {"name": "mail", "action": "x:send-email", "input": {"to": "a"}},
                },
            }
        ],
    }
    cfg = LiveConfigClient(ENV, client=_client(lambda r: httpx.Response(200, json=body)))
    (site,) = cfg.workflows()
    assert (site.type, site.name, site.part, site.every_minutes) == ("workflow", "Restock", "q", 60)


def test_fixture_connectors_serve_every_recording() -> None:
    now = datetime.now(UTC)
    conn = fixture_connectors(REPO_ROOT / "fixtures" / "dynatrace", now)
    assert len(conn.documents.list()) == 8
    assert {
        s.type for s in conn.config.workflows() + conn.config.alerts() + conn.config.slos()
    } == {
        "workflow",
        "alert",
        "slo",
    }
    assert "execute_dql" in {t.name for t in conn.mcp.list_tools()}
