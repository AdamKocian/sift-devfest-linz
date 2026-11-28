"""`SIFT_APPLY_TARGET=otelcol`: write the rule into the Collector config Sift manages.

The managed file (`SIFT_OTELCOL_CONFIG`, default data/otelcol/config.yaml) starts as a copy
of the base config (`SIFT_OTELCOL_BASE`, infra/otel-collector/dynatrace.yaml). For each
proposal Sift adds its components as `<type>/sift-<proposal id>`:

- filter / transform processors go into every pipeline of their signal, before `batch`;
- a count connector (logs -> metric) gets its own pipelines: `logs/sift-<id>` reads the same
  receivers as `logs` and exports to the connector, so it counts before the filter drops
  anything, and `metrics/sift-<id>` sends the counts through the metrics pipeline's
  processors and exporters (Dynatrace needs delta metrics: cumulative_to_delta stays).

A new file is checked with `otelcol validate` (`SIFT_OTELCOL_BIN`) before it replaces the old
one, then the Collector is told to reload (`SIFT_OTELCOL_RELOAD`):
- `pid:<pid or pid file>` sends SIGHUP. The Collector reloads its configuration on SIGHUP
  (otelcol/collector.go, `Run`: signal.Notify(..., SIGHUP) -> reloadConfiguration):
  https://github.com/open-telemetry/opentelemetry-collector/blob/main/otelcol/collector.go
  The file provider does not watch files, so a write alone changes nothing.
- `docker:<container>` runs `docker kill --signal=HUP <container>`.
- `none` only writes; the audit log says the Collector has not loaded it yet.
If validation or the reload fails, the previous file is put back and nothing is applied."""

from __future__ import annotations

import copy
import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from server.agent import rules
from server.apply import Applied, ApplyError, Reverted, check_approved, digest
from server.config import Config
from server.models import Proposal

HEADER = (
    "# Managed by Sift. Copied from {base} and extended with approved rules:\n"
    "# components named <type>/sift-<proposal id>. Sift rewrites this file; edit the base.\n"
)
SIGNAL_KEYS = {
    "log_conditions": "logs",
    "log_statements": "logs",
    "trace_conditions": "traces",
    "trace_statements": "traces",
    "metric_conditions": "metrics",
    "datapoint_conditions": "metrics",
    "metric_statements": "metrics",
}


def name_for(pid: str) -> str:
    return rules.otel_component_name(pid)


def _own(cid: str, pid: str) -> bool:
    return cid.split("/", 1)[-1] == name_for(pid)


def _signal(conf: dict[str, Any]) -> str:
    for key, sig in SIGNAL_KEYS.items():
        if key in conf:
            return sig
    raise ApplyError(f"cannot tell which pipeline this processor belongs to: {sorted(conf)}")


def strip_own(doc: dict[str, Any], pid: str) -> dict[str, Any]:
    """The config without anything this proposal added."""
    out = copy.deepcopy(doc)
    for section in ("processors", "connectors"):
        comps = out.get(section) or {}
        for cid in [c for c in comps if _own(c, pid)]:
            del comps[cid]
        if section in out and not comps:
            del out[section]
    pipelines = (out.get("service") or {}).get("pipelines") or {}
    for name in [n for n in pipelines if _own(n, pid)]:
        del pipelines[name]
    for pl in pipelines.values():
        for key in ("receivers", "processors", "exporters"):
            if key in pl:
                pl[key] = [c for c in pl[key] if not _own(c, pid)]
    return out


def merge(doc: dict[str, Any], snippet: str, pid: str) -> tuple[dict[str, Any], list[str]]:
    """Add the proposal's components to a Collector config. Returns (config, component ids)."""
    out = strip_own(doc, pid)
    snip = yaml.safe_load(snippet.partition("\n#")[0]) or {}
    pipelines = out.setdefault("service", {}).setdefault("pipelines", {})
    added: list[str] = []

    def of_type(sig: str) -> list[str]:
        return [n for n in pipelines if n.split("/", 1)[0] == sig and "/sift-" not in n]

    for cid, conf in (snip.get("processors") or {}).items():
        if not _own(cid, pid):
            raise ApplyError(f"{cid}: component ids must be <type>/{name_for(pid)}")
        sig = _signal(conf)
        targets = of_type(sig)
        if not targets:
            raise ApplyError(f"the managed config has no {sig} pipeline for {cid}")
        out.setdefault("processors", {})[cid] = conf
        for name in targets:
            procs = pipelines[name].setdefault("processors", [])
            at = procs.index("batch") if "batch" in procs else len(procs)
            procs.insert(at, cid)
        added.append(cid)
    for cid, conf in (snip.get("connectors") or {}).items():
        if not _own(cid, pid) or cid.split("/", 1)[0] != "count":
            raise ApplyError(f"{cid}: only count/{name_for(pid)} connectors are supported")
        if set(conf) != {"logs"}:
            raise ApplyError(f"{cid}: Sift wires count connectors for logs only")
        logs, metrics = pipelines.get("logs"), pipelines.get("metrics")
        if not logs or not metrics:
            raise ApplyError("a count connector needs a `logs` and a `metrics` pipeline")
        out.setdefault("connectors", {})[cid] = conf
        name = name_for(pid)
        pipelines[f"logs/{name}"] = {
            "receivers": list(logs["receivers"]),
            "processors": [c for c in logs.get("processors", []) if "/sift-" not in c],
            "exporters": [cid],
        }
        pipelines[f"metrics/{name}"] = {
            "receivers": [cid],
            "processors": [c for c in metrics.get("processors", []) if "/sift-" not in c],
            "exporters": list(metrics["exporters"]),
        }
        added.append(cid)
    if not added:
        raise ApplyError("the Collector snippet has no processors or connectors")
    return out, added


def dump(doc: dict[str, Any], base: Path) -> str:
    return HEADER.format(base=base.name) + yaml.safe_dump(
        doc, sort_keys=False, width=1000, allow_unicode=True
    )


def validate_text(text: str, binary: str | None) -> str:
    """'passed', 'failed: …' or 'not run: …' (`otelcol-contrib validate --config=<file>`).
    Exporters read ${env:DT_ENV_URL} / ${env:DT_API_TOKEN}; placeholders are used when they
    are not set, because validate checks the shape, not the tenant."""
    if not binary:
        return "not run: no collector binary (set SIFT_OTELCOL_BIN to run otelcol validate)"
    exe = shutil.which(binary) or binary
    if not Path(exe).exists():
        return f"not run: {binary} not found"
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(text)
        path = f.name
    env = {
        **os.environ,
        "DT_ENV_URL": os.environ.get("DT_ENV_URL") or "https://validate.invalid",
        "DT_API_TOKEN": os.environ.get("DT_API_TOKEN") or "validate-only",
    }
    try:
        p = subprocess.run(
            [exe, "validate", f"--config={path}"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    finally:
        Path(path).unlink(missing_ok=True)
    if p.returncode == 0:
        return "passed"
    lines = (p.stderr or p.stdout).strip().splitlines() or ["(no output)"]
    return "failed: " + lines[-1][:300]


class OtelcolApplier:
    target = "otelcol"

    def __init__(
        self,
        config_path: Path,
        base_path: Path,
        otelcol_bin: str | None = None,
        reload: str = "none",
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        kill: Callable[[int, int], None] = os.kill,
    ) -> None:
        self.path, self.base = config_path, base_path
        self.bin, self.reload_spec = otelcol_bin, reload
        self.run, self.kill = run, kill

    @classmethod
    def from_config(cls, cfg: Config) -> OtelcolApplier:
        return cls(cfg.otelcol_config, cfg.otelcol_base, cfg.otelcol_bin, cfg.otelcol_reload)

    # --- file ---------------------------------------------------------------------------

    def read(self) -> str:
        if self.path.exists():
            return self.path.read_text()
        if not self.base.exists():
            raise ApplyError(f"no managed Collector config at {self.path} and no base {self.base}")
        return self.base.read_text()

    def _write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, self.path)  # atomic: the Collector never reads half a file

    def _load(self, text: str) -> dict[str, Any]:
        doc = yaml.safe_load(text)
        if not isinstance(doc, dict):
            raise ApplyError(f"{self.path} is not a Collector config")
        return doc

    # --- reload -------------------------------------------------------------------------

    def reload(self) -> str:
        spec = self.reload_spec
        if spec == "none":
            return (
                "reload not configured (SIFT_OTELCOL_RELOAD=none): the Collector picks the "
                "file up on its next start or SIGHUP"
            )
        kind, _, arg = spec.partition(":")
        if kind == "pid":
            raw = arg if arg.isdigit() else Path(arg).read_text().strip()
            pid = int(raw)
            self.kill(pid, signal.SIGHUP)
            return f"sent SIGHUP to pid {pid}"
        if kind == "docker":
            p = self.run(
                ["docker", "kill", "--signal=HUP", arg], capture_output=True, text=True, timeout=30
            )
            if p.returncode != 0:
                raise ApplyError(f"docker kill --signal=HUP {arg} failed: {p.stderr.strip()}")
            return f"sent SIGHUP to container {arg}"
        raise ApplyError(f"unknown SIFT_OTELCOL_RELOAD {spec!r}")

    def _commit(self, text: str, previous: str, existed: bool) -> list[str]:
        """Validate, replace the file, reload; on failure put the previous file back."""
        result = validate_text(text, self.bin)
        log = [f"otelcol validate: {result}"]
        if result.startswith("failed"):
            raise ApplyError(f"otelcol validate {result}; nothing was written")
        self._write(text)
        try:
            log.append(self.reload())
        except (ApplyError, OSError, ValueError, subprocess.SubprocessError) as e:
            if existed:
                self._write(previous)
            else:
                self.path.unlink(missing_ok=True)
            raise ApplyError(f"Collector reload failed ({e}); previous config restored") from e
        return log

    # --- Applier ------------------------------------------------------------------------

    def snapshot(self, p: Proposal) -> dict[str, Any]:
        existed = self.path.exists()
        text = self.read()
        doc = self._load(text)
        clean = strip_own(doc, p.id)
        content = text if clean == doc else dump(clean, self.base)
        return {
            "target": self.target,
            "path": str(self.path),
            "existed": existed,
            "content": content,
            "sha256": digest(content),
        }

    def write(self, p: Proposal, snapshot: dict[str, Any]) -> Applied:
        check_approved(p)
        code = (p.rule.get("otel") or {}).get("code")
        if not code:
            raise ApplyError(f"{p.id} has no OpenTelemetry Collector rule")
        existed = self.path.exists()
        current = self.read()
        doc = self._load(current)
        new, ids = merge(doc, code, p.id)
        ref = {"path": str(self.path), "components": ids}
        if new == doc and existed:
            return Applied(
                self.target,
                f"Already in the Collector config ({', '.join(ids)}); nothing changed",
                {**ref, "sha256": digest(current)},
                changed=False,
            )
        text = dump(new, self.base)
        log = self._commit(text, current, existed)
        return Applied(
            self.target,
            f"Added {', '.join(ids)} to the Collector config {self.path.name}; " + log[-1],
            {**ref, "sha256": digest(text), "log": log},
            log=log,
        )

    def revert(self, p: Proposal) -> Reverted:
        snap = p.previous_state or {}
        ref = p.applied_ref or {}
        if not self.path.exists():
            return Reverted("The managed Collector config is gone; nothing to restore", "nothing")
        current = self.path.read_text()
        if ref.get("sha256") and digest(current) == ref["sha256"] and "content" in snap:
            log = self._commit(snap["content"], current, True)
            return Reverted(
                f"Restored the Collector config from the snapshot; {log[-1]}", "snapshot", log
            )
        doc = self._load(current)
        clean = strip_own(doc, p.id)
        if clean == doc:
            return Reverted(f"{name_for(p.id)} is no longer in the Collector config", "nothing")
        log = self._commit(dump(clean, self.base), current, True)
        return Reverted(
            "The Collector config changed after the rule was applied, so only this "
            f"proposal's components were removed; {log[-1]}",
            "removed_own",
            log,
        )
