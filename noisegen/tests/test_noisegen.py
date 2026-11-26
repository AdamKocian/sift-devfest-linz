"""noisegen without a collector: in-memory exporters, fixed seed."""

from __future__ import annotations

from collections import Counter
from typing import Any

from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from noisegen.emit import DELTA, Shop, Sinks, providers, run
from noisegen.world import LOGS, SPANS

SECONDS = 120
SCALE = 0.05


def emit(seed: int = 7, seconds: int = SECONDS) -> tuple[list[Any], list[Any], list[Any], Shop]:
    spans, logs = InMemorySpanExporter(), InMemoryLogRecordExporter()
    readers: list[InMemoryMetricReader] = []

    def reader() -> InMemoryMetricReader:
        r = InMemoryMetricReader(preferred_temporality=DELTA)
        readers.append(r)
        return r

    p = providers(Sinks(spans, logs, reader, batch=False))
    shop = Shop(p, SCALE, seed)
    run(shop, seconds, realtime=False)
    points = []
    for r in readers:
        data = r.get_metrics_data()
        for rm in data.resource_metrics if data else []:
            svc = rm.resource.attributes["service.name"]
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    points.extend((svc, m.name, dict(dp.attributes)) for dp in m.data.data_points)
    p.shutdown()
    return spans.get_finished_spans(), logs.get_finished_logs(), points, shop


def _log_rows(logs: list[Any]) -> list[tuple[str, str, str, str, dict[str, Any]]]:
    out = []
    for lg in logs:
        rec = lg.log_record
        attrs = dict(rec.attributes or {})
        svc = lg.resource.attributes["service.name"]
        out.append((svc, attrs.get("log.source"), rec.severity_text, str(rec.body), attrs))
    return out


def test_every_planted_case_is_emitted() -> None:
    spans, logs, points, _ = emit()
    rows = _log_rows(logs)

    def logs_of(svc: str, level: str, text: str) -> list[Any]:
        return [r for r in rows if r[0] == svc and r[2] == level and text in r[3]]

    assert logs_of("payment", "DEBUG", "Processing gateway payload")  # 1
    healthz = [s for s in spans if s.name == "GET /healthz"]  # 2
    assert healthz and all(s.attributes["http.route"] == "/healthz" for s in healthz)
    assert healthz[0].resource.attributes["service.name"] == "frontend"
    dup = logs_of("cart", "WARN", "Timeout calling inventory")  # 3
    assert len(dup) % 3 == 0 and len(dup) >= 3
    fns = Counter(r[4]["code.function.name"] for r in dup)
    assert set(fns) == {"InventoryClient.reserve", "CartService.reserve", "CartController.addItem"}
    assert len({r[3] for r in dup[:3]}) == 1  # the same stack trace three times
    assert dup[0][3].count("\n\tat ") == 44
    assert logs_of("inventory", "INFO", "heartbeat ok - worker")  # 4
    users = {a["user.id"] for svc, name, a in points if name == "http.server.request.duration"}
    assert len(users) > 200  # 5: one value per visitor
    assert all(
        svc == "frontend" for svc, name, _ in points if name == "http.server.request.duration"
    )
    pii = logs_of("email", "INFO", "Sent confirmation to user:")  # 6
    assert pii and "@example.com" in pii[0][3]
    weights = logs_of("recommendation", "ERROR", "ModelWeightsExpiredException")  # 7
    assert weights and weights[0][4]["exception.type"] == "ModelWeightsExpiredException"
    assert logs_of("checkout", "INFO", "Order placed") and logs_of(  # 8
        "checkout", "INFO", "Payment confirmed"
    )


def test_rates_follow_the_demo_world_shares() -> None:
    _, logs, _, shop = emit()
    expected = {f"log:{i}": x.records_day * SCALE / 86400 * SECONDS for i, x in enumerate(LOGS)}
    for i, x in enumerate(LOGS):
        key = f"log:{i}"
        per = 3 if x.case == 3 else 1  # case 3 counts timeouts, each logged three times
        assert abs(shop.counts[key] * per - expected[key]) <= per, (x, shop.counts[key])
    for i, s in enumerate(SPANS):
        assert abs(shop.counts[f"span:{i}"] - s.records_day * SCALE / 86400 * SECONDS) <= 1
    # payment DEBUG is the biggest log source by bytes, as in DEMO_WORLD
    size: Counter[tuple[str, str]] = Counter()
    for svc, _src, level, body, _ in _log_rows(logs):
        size[(svc, level)] += len(body)
    assert size.most_common(1)[0][0] == ("payment", "DEBUG")


def test_same_seed_same_telemetry() -> None:
    a = [r[3] for r in _log_rows(emit(seed=3, seconds=20)[1])]
    b = [r[3] for r in _log_rows(emit(seed=3, seconds=20)[1])]
    c = [r[3] for r in _log_rows(emit(seed=4, seconds=20)[1])]
    assert a == b and a != c


def test_failed_probes_keep_their_status() -> None:
    spans, *_ = emit(seconds=SECONDS)
    codes = Counter(
        s.attributes["http.response.status_code"] for s in spans if s.name == "GET /healthz"
    )
    assert codes[200] > 0 and set(codes) <= {200, 503}
