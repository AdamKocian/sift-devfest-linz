"""Recorded Dynatrace responses on disk (fixtures/dynatrace/).

Every file holds one raw API response plus the moment it was recorded. On replay all
ISO-8601 UTC timestamps are shifted by (now - recorded_at), so a recording made last week
still says "this dashboard ran 25 minutes ago". The fixture connectors hand the shifted
body to the same parse functions the live connectors use."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from server.connectors.base import FixtureMissingError

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?Z$")


def parse_ts(value: str) -> datetime:
    """'2026-11-26T10:00:00.123456789Z' -> aware UTC datetime (sub-microsecond dropped)."""
    m = _TS.match(value)
    if not m:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    frac = (m.group(2) or ".0")[1:7].ljust(6, "0")
    return datetime.fromisoformat(f"{m.group(1)}.{frac}+00:00")


def format_ts(value: datetime) -> str:
    """Grail style: nanosecond precision, UTC, 'Z'."""
    v = value.astimezone(UTC)
    return v.strftime("%Y-%m-%dT%H:%M:%S.") + f"{v.microsecond:06d}000Z"


def shift(obj: Any, delta: timedelta) -> Any:
    """Return a copy of `obj` with every ISO UTC timestamp string moved by `delta`."""
    if isinstance(obj, dict):
        return {k: shift(v, delta) for k, v in obj.items()}
    if isinstance(obj, list):
        return [shift(v, delta) for v in obj]
    if isinstance(obj, str) and _TS.match(obj):
        return format_ts(parse_ts(obj) + delta)
    return obj


def safe_name(name: str) -> str:
    """Query or document names become file names: 'log_sample:payment/app/DEBUG' ->
    'log_sample__payment_app_debug'."""
    out = re.sub(r"[^a-z0-9]+", "_", name.lower().replace(":", "__")).strip("_")
    return out or "unnamed"


class FixtureStore:
    def __init__(self, root: Path, now: datetime | None = None) -> None:
        self.root = root
        self.now = now

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def read(self, *parts: str) -> dict[str, Any]:
        p = self.path(*parts)
        if not p.exists():
            raise FixtureMissingError(
                f"no recorded response at {p.relative_to(self.root.parent)}; "
                "run `make fixtures` against a tenant or `make fixtures-synth`"
            )
        data = json.loads(p.read_text())
        recorded = data.get("recorded_at")
        if self.now is not None and recorded:
            data = shift(data, self.now - parse_ts(recorded))
            data["recorded_at"] = recorded
        return data

    def write(self, data: dict[str, Any], *parts: str) -> Path:
        p = self.path(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
        return p
