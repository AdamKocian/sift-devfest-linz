"""noisegen: send the noisy-shop world over OTLP.

uv run noisegen run --seconds 900                  # 15 min, real time, to the collector
uv run noisegen run --seconds 60 --fast --exporter console
uv run noisegen setup                              # dashboard + notebook + query executions
"""

from __future__ import annotations

import argparse
import os
import sys

from opentelemetry.sdk._logs.export import ConsoleLogRecordExporter
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.trace.export import ConsoleSpanExporter

from noisegen.emit import DELTA, Shop, Sinks, providers, run
from noisegen.world import planted_cases


def otlp_sinks(endpoint: str, interval_ms: int) -> Sinks:
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    base = endpoint.rstrip("/")
    return Sinks(
        span_exporter=OTLPSpanExporter(endpoint=f"{base}/v1/traces"),
        log_exporter=OTLPLogExporter(endpoint=f"{base}/v1/logs"),
        metric_reader=lambda: PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{base}/v1/metrics", preferred_temporality=DELTA),
            export_interval_millis=interval_ms,
        ),
    )


def console_sinks(interval_ms: int) -> Sinks:
    return Sinks(
        span_exporter=ConsoleSpanExporter(),
        log_exporter=ConsoleLogRecordExporter(),
        metric_reader=lambda: PeriodicExportingMetricReader(
            ConsoleMetricExporter(preferred_temporality=DELTA),
            export_interval_millis=interval_ms,
        ),
        batch=False,
    )


def cmd_run(args: argparse.Namespace) -> int:
    if args.exporter == "otlp":
        sinks = otlp_sinks(args.endpoint, args.metric_interval_ms)
    else:
        sinks = console_sinks(args.metric_interval_ms)
    p = providers(sinks)
    shop = Shop(p, args.scale, args.seed)
    print(
        f"noisegen: {args.seconds}s at scale {args.scale:g} (seed {args.seed}) -> "
        f"{args.exporter} {args.endpoint if args.exporter == 'otlp' else ''}",
        file=sys.stderr,
    )
    for n, label in sorted(planted_cases().items()):
        print(f"  case {n}: {label}", file=sys.stderr)
    try:
        run(shop, args.seconds, realtime=not args.fast)
    finally:
        p.flush()
        p.shutdown()
    total = sum(shop.counts.values())
    print(f"noisegen: emitted {total:,} records/data points", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="noisegen")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="emit telemetry")
    r.add_argument("--seconds", type=int, default=900)
    r.add_argument(
        "--scale",
        type=float,
        default=float(os.getenv("NOISEGEN_SCALE", "0.002")),
        help="fraction of the demo world's volume (0.002 = about 80 KB/min)",
    )
    r.add_argument("--seed", type=int, default=int(os.getenv("NOISEGEN_SEED", "42")))
    r.add_argument("--exporter", choices=("otlp", "console"), default="otlp")
    r.add_argument(
        "--endpoint",
        default=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"),
    )
    r.add_argument("--metric-interval-ms", type=int, default=60_000)
    r.add_argument("--fast", action="store_true", help="do not wait between simulated seconds")
    s = sub.add_parser("setup", help="create the case-8 dashboard/notebook and run its queries")
    s.add_argument("--fixtures", default=os.getenv("SIFT_FIXTURES", "../fixtures"))
    s.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "run":
        return cmd_run(args)
    from noisegen.setup_tenant import setup

    return setup(args.fixtures, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
