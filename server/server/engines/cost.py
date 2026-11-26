"""Cost model: what a GiB of telemetry costs and what a rule saves.

Two modes (settings key `cost_model`):
- all_in (default): one € per GiB per data type covering ingest, retention and queries.
- split: ingest € per GiB + retain € per GiB-day × retention days. Dynatrace bills Ingest &
  Process on the raw bytes sent (https://docs.dynatrace.com/docs/license/capabilities/log-analytics/dps-log-ingest)
  and Retain per GiB-day after filtering (https://docs.dynatrace.com/docs/license/capabilities/log-analytics/dps-log-retain);
  "filtering generates usage for Ingest & Process, but not for Retain"
  (https://docs.dynatrace.com/docs/license/capabilities/log-analytics). So an OpenPipeline
  drop saves Retain only, a drop in the OTel Collector before Dynatrace saves both.
All rates are the user's own numbers, never a Dynatrace price list."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DAYS_PER_MONTH = 30
TARGETS = ("openpipeline", "otelcol")


@dataclass(frozen=True)
class CostModel:
    mode: str = "all_in"
    all_in: dict[str, float] = field(default_factory=dict)  # € / GiB
    ingest: dict[str, float] = field(default_factory=dict)  # € / GiB
    retain: dict[str, float] = field(default_factory=dict)  # € / GiB-day
    retention_days: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> CostModel:
        cm = settings.get("cost_model") or {}
        split = cm.get("split") or {}
        mode = cm.get("mode", "all_in")
        if mode not in ("all_in", "split"):
            raise ValueError(f"cost_model.mode must be all_in or split, got {mode!r}")
        return cls(
            mode=mode,
            all_in={k: float(v) for k, v in (settings.get("rates_eur_per_gib") or {}).items()},
            ingest={k: float(v) for k, v in (split.get("ingest_eur_per_gib") or {}).items()},
            retain={k: float(v) for k, v in (split.get("retain_eur_per_gib_day") or {}).items()},
            retention_days={k: float(v) for k, v in (split.get("retention_days") or {}).items()},
        )

    def _ingest_month(self, data_type: str, gib_day: float) -> float:
        return gib_day * DAYS_PER_MONTH * self.ingest.get(data_type, 0.0)

    def _retain_month(self, data_type: str, gib_day: float) -> float:
        # Steady state: gib_day × retention days are stored on every day of the month.
        stored = gib_day * self.retention_days.get(data_type, 0.0)
        return stored * self.retain.get(data_type, 0.0) * DAYS_PER_MONTH

    def monthly(self, data_type: str, gib_day: float) -> float:
        if self.mode == "all_in":
            return gib_day * DAYS_PER_MONTH * self.all_in.get(data_type, 0.0)
        return self._ingest_month(data_type, gib_day) + self._retain_month(data_type, gib_day)

    def saving(self, data_type: str, gib_day: float, target: str | None = None) -> float:
        """Monthly saving of removing `gib_day`. In split mode an OpenPipeline rule only
        saves Retain; the Collector (before ingest) saves Ingest and Retain."""
        if self.mode == "all_in" or target is None:
            return self.monthly(data_type, gib_day)
        if target not in TARGETS:
            raise ValueError(f"unknown apply target {target!r}")
        retain = self._retain_month(data_type, gib_day)
        return (
            retain if target == "openpipeline" else retain + self._ingest_month(data_type, gib_day)
        )
