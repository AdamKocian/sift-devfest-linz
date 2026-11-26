"""Emit the noisy-shop world with the OpenTelemetry SDK.

Each simulated second every signal emits `records_day * scale / 86400` records on average.
Counts use a carry accumulator, so they are exact and need no randomness; message
contents come from a seeded RNG, so the same seed produces the same telemetry.

Metrics use delta temporality: Dynatrace only accepts delta
(https://docs.dynatrace.com/docs/ingest-from/opentelemetry/otlp-api/about-metrics-ingest)."""

from __future__ import annotations

import random
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import (
    Counter,
    Histogram,
    MeterProvider,
    ObservableCounter,
    ObservableGauge,
    ObservableUpDownCounter,
    UpDownCounter,
)
from opentelemetry.sdk.metrics.export import AggregationTemporality, MetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode

from noisegen.world import (
    CART_FUNCTIONS,
    GAUGES,
    HTTP_DURATION_RECORDS_DAY,
    LOGS,
    SERVICES,
    SPANS,
    LogSignal,
)

DELTA = {
    Counter: AggregationTemporality.DELTA,
    UpDownCounter: AggregationTemporality.DELTA,
    Histogram: AggregationTemporality.DELTA,
    ObservableCounter: AggregationTemporality.DELTA,
    ObservableUpDownCounter: AggregationTemporality.DELTA,
    ObservableGauge: AggregationTemporality.DELTA,
}
SEVERITY = {
    "DEBUG": SeverityNumber.DEBUG,
    "INFO": SeverityNumber.INFO,
    "WARN": SeverityNumber.WARN,
    "ERROR": SeverityNumber.ERROR,
}
NAMES = ("alice", "bob", "carla", "deniz", "emil", "fatima", "georg", "hana", "ivan", "jana")
_PH = re.compile(r"\{(\w+)(?::([^}]*))?\}")


def render(template: str, rng: random.Random, worker: int = 0) -> str:
    def sub(m: re.Match[str]) -> str:
        kind, arg = m.group(1), m.group(2) or ""
        if kind == "int":
            lo, hi = (int(x) for x in arg.split("-"))
            return str(rng.randint(lo, hi))
        if kind == "hex":
            return "".join(rng.choice("0123456789abcdef") for _ in range(int(arg)))
        if kind == "uuid":
            return str(uuid.UUID(int=rng.getrandbits(128), version=4))
        if kind == "email":
            return f"{rng.choice(NAMES)}{rng.randint(1, 99)}@example.com"
        if kind == "choice":
            return rng.choice(arg.split("|"))
        if kind == "worker":
            return str(worker)
        if kind == "trace":
            return "".join(
                f"\n\tat shop.net.Frame{i}.call(Frame{i}.java:{100 + i})" for i in range(44)
            )
        raise ValueError(f"unknown placeholder {kind}")

    return _PH.sub(sub, template)


@dataclass
class Sinks:
    """Where telemetry goes: exporters for spans and logs, a reader factory for metrics."""

    span_exporter: SpanExporter
    log_exporter: LogRecordExporter
    metric_reader: Callable[[], MetricReader]
    batch: bool = True


@dataclass
class Providers:
    tracers: dict[str, Any] = field(default_factory=dict)
    loggers: dict[str, Any] = field(default_factory=dict)
    meters: dict[str, Any] = field(default_factory=dict)
    all: list[Any] = field(default_factory=list)

    def flush(self) -> None:
        for p in self.all:
            p.force_flush()

    def shutdown(self) -> None:
        for p in self.all:
            p.shutdown()


def providers(sinks: Sinks, namespace: str = "noisy-shop") -> Providers:
    """One resource per service, so service.name is a resource attribute as in real apps."""
    out = Providers()
    for svc in SERVICES:
        res = Resource.create(
            {
                "service.name": svc,
                "service.namespace": namespace,
                "deployment.environment.name": "demo",
            }
        )
        tp = TracerProvider(resource=res)
        tp.add_span_processor(
            BatchSpanProcessor(sinks.span_exporter)
            if sinks.batch
            else SimpleSpanProcessor(sinks.span_exporter)
        )
        lp = LoggerProvider(resource=res)
        lp.add_log_record_processor(
            BatchLogRecordProcessor(sinks.log_exporter)
            if sinks.batch
            else SimpleLogRecordProcessor(sinks.log_exporter)
        )
        mp = MeterProvider(resource=res, metric_readers=[sinks.metric_reader()])
        out.tracers[svc] = tp.get_tracer("noisegen")
        out.loggers[svc] = lp.get_logger("noisegen")
        out.meters[svc] = mp.get_meter("noisegen")
        out.all.extend([tp, lp, mp])
    return out


class Shop:
    def __init__(self, p: Providers, scale: float, seed: int, users: int = 1500) -> None:
        self.p = p
        self.scale = scale
        self.rng = random.Random(seed)
        self.carry: dict[str, float] = {}
        self.users = [
            str(uuid.UUID(int=self.rng.getrandbits(128), version=4)) for _ in range(users)
        ]
        self.counts: dict[str, int] = {}
        self.duration = p.meters["frontend"].create_histogram(
            "http.server.request.duration", unit="s", description="HTTP server request duration"
        )
        self.gauges = {
            (svc, name): (p.meters[svc].create_gauge(name, unit=unit), attrs)
            for svc, name, unit, attrs in GAUGES
        }

    def _n(self, key: str, per_day: float) -> int:
        acc = self.carry.get(key, 0.0) + per_day * self.scale / 86400
        n = int(acc)
        self.carry[key] = acc - n
        self.counts[key] = self.counts.get(key, 0) + n
        return n

    def _log(self, x: LogSignal, body: str, attrs: dict[str, Any]) -> None:
        self.p.loggers[x.service].emit(
            timestamp=time.time_ns(),
            severity_text=x.level,
            severity_number=SEVERITY[x.level],
            body=body,
            attributes={"log.source": x.source, **x.attributes, **attrs},
        )

    def second(self) -> None:
        """Emit one simulated second of telemetry."""
        rng = self.rng
        for i, x in enumerate(LOGS):
            key = f"log:{i}"
            if x.case == 3:
                # One timeout is logged three times with the identical stack trace.
                for _ in range(self._n(key, x.records_day / 3)):
                    body = render(x.messages[0], rng)
                    for fn in CART_FUNCTIONS:
                        self._log(x, body, {"code.function.name": fn})
                continue
            for _ in range(self._n(key, x.records_day)):
                attrs: dict[str, Any] = {}
                worker = 0
                if x.case == 4:
                    worker = rng.randint(1, 10)
                    attrs["worker.id"] = worker
                self._log(x, render(rng.choice(x.messages), rng, worker), attrs)
        for i, s in enumerate(SPANS):
            for _ in range(self._n(f"span:{i}", s.records_day)):
                self._span(s.service, s.name, dict(s.attributes), s.case)
        for _ in range(self._n("metric:http.duration", HTTP_DURATION_RECORDS_DAY)):
            self.duration.record(
                rng.uniform(0.002, 0.8),
                {
                    "http.route": rng.choice(
                        ("/api/products", "/api/cart", "/checkout", "/product")
                    ),
                    "http.request.method": rng.choice(("GET", "POST")),
                    "user.id": rng.choice(self.users),
                },
            )
        for (_svc, name), (gauge, sets) in self.gauges.items():
            for attrs in sets:
                gauge.set(self._gauge_value(name), attrs)

    def _gauge_value(self, name: str) -> float:
        r = self.rng
        return {
            "jvm.memory.used": r.uniform(2e8, 6e8),
            "db.client.connection.count": float(r.randint(1, 20)),
            "inventory.stock.level": float(r.randint(0, 400)),
            "process.cpu.utilization": r.uniform(0.2, 0.95),
        }[name]

    def _span(self, service: str, name: str, attrs: dict[str, Any], case: int | None) -> None:
        rng = self.rng
        kind = SpanKind.SERVER if name.startswith(("GET", "POST")) else SpanKind.INTERNAL
        status_code = None
        if "http.route" in attrs:
            failed = rng.random() < (0.01 if case == 2 else 0.003)
            status_code = 503 if failed else (201 if name.startswith("POST") else 200)
            attrs["http.response.status_code"] = status_code
        if case == 2:
            attrs["k8s.pod.name"] = f"frontend-7c9f-{rng.choice(('a1b2', 'c3d4'))}"
        start = time.time_ns()
        span = self.p.tracers[service].start_span(
            name, kind=kind, attributes=attrs, start_time=start
        )
        if status_code and status_code >= 500:
            span.set_status(Status(StatusCode.ERROR))
        span.end(end_time=start + int(rng.uniform(0.2, 400) * 1e6))


def run(
    shop: Shop,
    seconds: int,
    realtime: bool,
    on_tick: Callable[[int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    for t in range(seconds):
        began = time.monotonic()
        shop.second()
        if on_tick:
            on_tick(t)
        if realtime:
            sleep(max(0.0, 1.0 - (time.monotonic() - began)))
