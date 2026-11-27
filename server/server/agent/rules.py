"""Rules: what a proposal changes, written for both apply targets from one structured
matcher, plus the checks `validate_rule` runs on them.

The agent never writes DQL or OTTL itself. It picks a signal, an action and a few options
(a phrase to keep, a status code, a metric name); this module turns that into
- an OpenPipeline processor (DQL matcher) and
- an OpenTelemetry Collector snippet (OTTL conditions / statements),
so both targets act on exactly the same records and the dry run measures that matcher.

Sources:
- OpenPipeline stages and processors: https://docs.dynatrace.com/docs/platform/openpipeline/concepts/processing
  The configuration-API JSON keys are NOT verified (no tenant yet): TODO(verify).
- Collector components (opentelemetry-collector-contrib READMEs): filter processor
  (`log_conditions` / `trace_conditions`), transform processor (`*_statements` with
  `context`, `conditions`, `statements`), count connector. Same shapes as the reviewed
  fixtures in fixtures/proposals.json.
- `log.source` is sent by noisegen as a log attribute (noisegen/noisegen/emit.py)."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from server.engine import World
from server.engines import patterns
from server.models import Signal

ACTIONS: dict[str, tuple[str, ...]] = {
    "drop": ("log_source", "log_pattern", "span"),
    "drop_field": ("metric_dimension", "span_attribute"),
    "logs_to_metric": ("log_source", "log_pattern"),
    "redact": ("log_source", "log_pattern"),
}
# In the domain model but not drafted yet: sampling and lowering the level need a routing
# step in the Collector and an OpenPipeline processor we have not verified.
NOT_YET = {
    "sample": "Sampling is not drafted yet. Use drop with a narrower matcher.",
    "lower_level": "Lowering the level is not drafted yet.",
}
REDACTIONS = {
    # (OTTL regex for replace_pattern, Dynatrace Pattern Language for replacePattern)
    # DPL syntax: TODO(verify) https://docs.dynatrace.com/docs/platform/grail/dynatrace-pattern-language
    "email": (
        "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}",
        "[a-zA-Z0-9._%+-]+ '@' [a-zA-Z0-9.-]+",
    ),
}
SEVERITY = {  # OTTL severity range [lo, hi) per Dynatrace loglevel
    "TRACE": ("SEVERITY_NUMBER_TRACE", "SEVERITY_NUMBER_DEBUG"),
    "DEBUG": ("SEVERITY_NUMBER_DEBUG", "SEVERITY_NUMBER_INFO"),
    "INFO": ("SEVERITY_NUMBER_INFO", "SEVERITY_NUMBER_WARN"),
    "WARN": ("SEVERITY_NUMBER_WARN", "SEVERITY_NUMBER_ERROR"),
    "ERROR": ("SEVERITY_NUMBER_ERROR", "SEVERITY_NUMBER_FATAL"),
    "FATAL": ("SEVERITY_NUMBER_FATAL", None),
}
OP_NOTE = (
    "Stage and processor names follow the OpenPipeline processing docs. The exact "
    "configuration-API JSON keys are not verified yet. TODO(verify): "
    "https://docs.dynatrace.com/docs/platform/openpipeline/concepts/processing"
)
METRIC_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){1,6}$")


class DraftError(ValueError):
    """The requested rule cannot be drafted; the message goes back to the agent."""


def _q(value: str) -> str:
    """A string literal valid in both DQL and OTTL."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def phrase_regex(phrase: str) -> str:
    """A case-insensitive regex for a matchesPhrase() phrase, without backslashes (so it
    survives YAML and OTTL string escaping): metacharacters go into [x] classes."""
    words = phrase.split()
    esc = ["".join(f"[{c}]" if c in ".+*?()|{}$^" else c for c in w) for w in words]
    return "(?i)" + " +".join(esc)


@dataclass(frozen=True)
class Cond:
    field: str  # service.name | log.source | loglevel | content | span.name | metric.key | ...
    op: str  # eq | ne | phrase | not_phrase
    value: str | int

    def dql(self) -> str:
        v = self.value if isinstance(self.value, int) else _q(str(self.value))
        if self.op == "eq":
            return f"{self.field} == {v}"
        if self.op == "ne":
            return f"{self.field} != {v}"
        if self.op == "phrase":
            return f"matchesPhrase({self.field}, {v})"
        if self.op == "not_phrase":
            return f"not matchesPhrase({self.field}, {v})"
        raise ValueError(self.op)

    def ottl(self, data_type: str) -> str:
        f, v = self.field, self.value
        lit = v if isinstance(v, int) else _q(str(v))
        if f == "service.name":
            path = 'resource.attributes["service.name"]'
        elif f == "loglevel":
            lo, hi = SEVERITY[str(v)]
            expr = f"log.severity_number >= {lo}" + (
                f" and log.severity_number < {hi}" if hi else ""
            )
            return expr if self.op == "eq" else f"not ({expr})"
        elif f == "content":
            m = f"IsMatch(log.body, {_q(phrase_regex(str(v)))})"
            return m if self.op == "phrase" else f"not {m}"
        elif f == "span.name":
            path = "span.name"
        elif f == "metric.key":
            path = "metric.name"
        elif data_type == "logs":
            path = f"log.attributes[{_q(f)}]"
        else:
            path = f"span.attributes[{_q(f)}]"
        return f"{path} {'==' if self.op == 'eq' else '!='} {lit}"


def dql_and(conds: list[Cond]) -> str:
    return " and ".join(c.dql() for c in conds)


def ottl_and(conds: list[Cond], data_type: str) -> str:
    return " and ".join(c.ottl(data_type) for c in conds)


# --- which records a signal is ----------------------------------------------------------


def parent(w: World, sig: Signal) -> Signal | None:
    return w.signals.get(sig.parent_id) if sig.parent_id else None


def target_conds(w: World, sig: Signal) -> list[Cond]:
    """The DQL/OTTL conditions that select exactly this signal's records."""
    if sig.kind == "log_source":
        out = [Cond("service.name", "eq", sig.service)]
        if sig.source:
            out.append(Cond("log.source", "eq", sig.source))
        if sig.level:
            out.append(Cond("loglevel", "eq", sig.level))
        return out
    if sig.kind == "log_pattern":
        p = parent(w, sig)
        assert p is not None
        return [*target_conds(w, p), Cond("content", "phrase", patterns.phrase(sig.key))]
    if sig.kind == "span":
        return [Cond("service.name", "eq", sig.service), Cond("span.name", "eq", sig.key)]
    if sig.kind in ("span_attribute", "metric_dimension"):
        p = parent(w, sig)
        assert p is not None
        return target_conds(w, p)
    if sig.kind == "metric":
        return [Cond("metric.key", "eq", sig.key)]
    raise DraftError(f"unknown signal kind {sig.kind}")


def scope_conds(sig: Signal) -> list[Cond]:
    """What the dry run compares against: the service's logs or spans."""
    return [Cond("service.name", "eq", sig.service)]


def engine_scope(w: World, sig: Signal) -> dict[str, Any]:
    """The scope `engine.dry_run` measures the proposal on (same as fixtures/proposals.json)."""
    if sig.kind in ("metric_dimension", "span_attribute"):
        return {"signal": sig.parent_id}
    if sig.kind == "metric":
        return {"signal": sig.id}
    return {"service": sig.service, "data_type": sig.data_type}


# --- drafts -----------------------------------------------------------------------------


@dataclass
class Draft:
    id: str
    signal_id: str
    action: str
    title: str
    data_type: str
    target: list[Cond]
    matcher: list[Cond]
    params: dict[str, Any]
    openpipeline: dict[str, Any]
    otel: dict[str, Any]
    scope: dict[str, Any]
    validation: dict[str, Any] | None = None
    reduction_ratio: float | None = None
    added_gib_day: float = 0.0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """What the agent sees: the rule in both targets, briefly."""
        return {
            "draft_id": self.id,
            "signal_id": self.signal_id,
            "action": self.action,
            "title": self.title,
            "matcher_dql": self.openpipeline["matcher"],
            "openpipeline": {
                "stage": self.openpipeline["stage"],
                "processor": self.openpipeline["processor"],
            },
            "otel_collector_yaml": self.otel["code"],
            "next": "validate_rule(draft_id)",
        }

    def digest(self) -> str:
        blob = json.dumps(
            [self.action, [asdict(c) for c in self.matcher], self.params], sort_keys=True
        )
        return hashlib.sha1(blob.encode()).hexdigest()[:8]


def _slug(sig: Signal) -> str:
    base = re.sub(r"^(log|span|metric)-", "", sig.id)
    return re.sub(r"[^a-z0-9-]+", "-", base.lower())[:48].strip("-")


def _pipeline(sig: Signal) -> str:
    return f"{sig.data_type} · {sig.service}"


def _yaml(doc: dict[str, Any], comment: str | None = None) -> str:
    text = yaml.safe_dump(doc, sort_keys=False, width=1000, allow_unicode=True)
    return text + (f"\n{comment}\n" if comment else "")


def build(
    w: World,
    sig: Signal,
    action: str,
    draft_id: str,
    *,
    title: str = "",
    keep_phrase: str = "",
    only_status_code: int = 0,
    metric_name: str = "",
    redact: str = "email",
) -> Draft:
    if action in NOT_YET:
        raise DraftError(NOT_YET[action])
    if action not in ACTIONS:
        raise DraftError(f"unknown action {action!r}; use one of {sorted(ACTIONS)}")
    if sig.kind not in ACTIONS[action]:
        raise DraftError(
            f"{action} works on {', '.join(ACTIONS[action])}, not on a {sig.kind} ({sig.id})"
        )
    if keep_phrase and sig.data_type != "logs":
        raise DraftError("keep_phrase only applies to logs")
    if only_status_code and sig.data_type != "spans":
        raise DraftError("only_status_code only applies to spans")
    target = target_conds(w, sig)
    matcher = list(target)
    if keep_phrase:
        matcher.append(Cond("content", "not_phrase", keep_phrase))
    if only_status_code:
        matcher.append(Cond("http.response.status_code", "eq", int(only_status_code)))
    slug = _slug(sig)
    name = f"sift-{slug}"
    m_dql = dql_and(matcher)
    title = title.strip() or _default_title(sig, action)
    params: dict[str, Any] = {
        k: v
        for k, v in {
            "keep_phrase": keep_phrase,
            "only_status_code": only_status_code,
            "metric_name": metric_name,
            "redact": redact if action == "redact" else "",
        }.items()
        if v
    }
    dt = sig.data_type
    if action == "drop":
        key = {"logs": "log_conditions", "spans": "trace_conditions"}[dt]
        otel = _yaml(
            {
                "processors": {
                    f"filter/{name}": {"error_mode": "ignore", key: [ottl_and(matcher, dt)]}
                }
            }
        )
        op = {
            "stage": "Processing",
            "processor": "Drop record",
            "matcher": m_dql,
            "code": json.dumps(
                {
                    "pipeline": _pipeline(sig),
                    "stage": "processing",
                    "processor": {"type": "drop", "name": f"Sift · {title}", "matcher": m_dql},
                },
                indent=2,
                ensure_ascii=False,
            ),
        }
    elif action == "drop_field":
        fld = sig.key
        if dt == "metrics":
            stmt = {
                "context": "datapoint",
                "conditions": [ottl_and(matcher, dt)],
                "statements": [f"delete_key(datapoint.attributes, {_q(fld)})"],
            }
            otel = _yaml(
                {
                    "processors": {
                        f"transform/{name}": {"error_mode": "ignore", "metric_statements": [stmt]}
                    }
                }
            )
        else:
            stmt = {
                "context": "span",
                "conditions": [ottl_and(matcher, dt)],
                "statements": [f"delete_key(span.attributes, {_q(fld)})"],
            }
            otel = _yaml(
                {
                    "processors": {
                        f"transform/{name}": {"error_mode": "ignore", "trace_statements": [stmt]}
                    }
                }
            )
        op = {
            "stage": "Processing",
            "processor": "Remove fields",
            "matcher": m_dql,
            "code": json.dumps(
                {
                    "pipeline": _pipeline(sig),
                    "stage": "processing",
                    "processor": {
                        "type": "fieldsRemove",
                        "name": f"Sift · {title}",
                        "matcher": m_dql,
                        "fields": [fld],
                    },
                },
                indent=2,
                ensure_ascii=False,
            ),
        }
    elif action == "logs_to_metric":
        metric = metric_name or _default_metric(sig)
        if not METRIC_NAME.match(metric):
            raise DraftError(f"metric_name {metric!r} must look like service.thing.count")
        params["metric_name"] = metric
        cond = ottl_and(matcher, dt)
        otel = _yaml(
            {
                "connectors": {
                    f"count/{name}": {
                        "logs": {metric: {"description": f"Sift: {title}", "conditions": [cond]}}
                    }
                },
                "processors": {
                    f"filter/{name}": {"error_mode": "ignore", "log_conditions": [cond]}
                },
            },
            "# Wire the count connector as an exporter of the logs pipeline (before the filter)\n"
            "# and as a receiver of a metrics pipeline.",
        )
        op = {
            "stage": "Metric extraction + Storage",
            "processor": "Counter metric, then No storage assignment",
            "matcher": m_dql,
            "code": json.dumps(
                {
                    "pipeline": _pipeline(sig),
                    "metricExtraction": {
                        "type": "counter",
                        "name": f"Sift · count {metric}",
                        "metric": f"log.{metric}",
                        "dimensions": ["service.name"],
                        "matcher": m_dql,
                    },
                    "storage": {
                        "type": "noStorage",
                        "name": f"Sift · {title}",
                        "matcher": m_dql,
                    },
                },
                indent=2,
                ensure_ascii=False,
            ),
        }
    else:  # redact
        if redact not in REDACTIONS:
            raise DraftError(f"redact supports {sorted(REDACTIONS)}")
        regex, dpl = REDACTIONS[redact]
        stmt = {
            "context": "log",
            "conditions": [ottl_and(matcher, dt)],
            "statements": [f"replace_pattern(log.body, {_q(regex)}, {_q('***@***')})"],
        }
        otel = _yaml(
            {
                "processors": {
                    f"transform/{name}": {"error_mode": "ignore", "log_statements": [stmt]}
                }
            }
        )
        op = {
            "stage": "Processing",
            "processor": "DQL",
            "matcher": m_dql,
            "code": json.dumps(
                {
                    "pipeline": _pipeline(sig),
                    "stage": "processing",
                    "processor": {
                        "type": "dql",
                        "name": f"Sift · {title}",
                        "matcher": m_dql,
                        "script": "fieldsAdd content = replacePattern(content, "
                        f'{_q(dpl)}, "***@***")',
                    },
                },
                indent=2,
                ensure_ascii=False,
            ),
        }
    op["note"] = OP_NOTE
    return Draft(
        id=draft_id,
        signal_id=sig.id,
        action=action,
        title=title,
        data_type=dt,
        target=target,
        matcher=matcher,
        params=params,
        openpipeline=op,
        otel={"code": otel, "note": ""},
        scope=engine_scope(w, sig),
    )


def _default_title(sig: Signal, action: str) -> str:
    what = {
        "log_source": f"{sig.level} logs from {sig.service}",
        "log_pattern": f"“{patterns.phrase(sig.key)}” logs from {sig.service}",
        "span": f"{sig.key} spans from {sig.service}",
        "span_attribute": f"{sig.key} from {sig.service} spans",
        "metric_dimension": f"{sig.key} from its metric",
    }.get(sig.kind, sig.key)
    verb = {
        "drop": "Drop",
        "drop_field": "Remove",
        "logs_to_metric": "Count",
        "redact": "Mask emails in",
    }[action]
    return f"{verb} {what}"


def _default_metric(sig: Signal) -> str:
    words = re.findall(r"[a-z0-9]+", patterns.phrase(sig.key).lower() or sig.key.lower())
    return f"{sig.service}.{'_'.join(words[:3]) or 'lines'}.count"


# --- OpenTelemetry Collector checks -----------------------------------------------------

KNOWN_COMPONENTS = {"processors": {"filter", "transform"}, "connectors": {"count"}}
STATEMENT_KEYS = {"log_statements", "trace_statements", "metric_statements"}
CONDITION_KEYS = {"log_conditions", "trace_conditions", "metric_conditions"}
OTTL_FUNCS = {"IsMatch", "delete_key", "replace_pattern", "not"}
OTTL_PATHS = re.compile(r"^(resource|log|span|metric|datapoint)\.")


def _ottl_problems(expr: str) -> list[str]:
    out: list[str] = []
    stripped = re.sub(r'"(?:\\.|[^"\\])*"', '""', expr)
    if stripped.count('"') % 2:
        out.append(f"unbalanced quotes in {expr!r}")
    for a, b in ("()", "[]"):
        if stripped.count(a) != stripped.count(b):
            out.append(f"unbalanced {a}{b} in {expr!r}")
    for fn in re.findall(r"\b([A-Za-z_]+)\(", stripped):
        if fn not in OTTL_FUNCS:
            out.append(f"unknown OTTL function {fn}() in {expr!r}")
    for tok in re.findall(r"\b[a-z_]+\.[a-z_.]+", stripped):
        if not OTTL_PATHS.match(tok) and not tok.startswith("SEVERITY"):
            out.append(f"unknown OTTL path {tok} in {expr!r}")
    return out


def otel_structure_problems(code: str) -> list[str]:
    """Checks the snippet's shape without a collector: YAML, component types and
    settings keys, and that each OTTL string is well formed."""
    try:
        doc = yaml.safe_load(code)
    except yaml.YAMLError as e:
        return [f"not valid YAML: {e}"]
    if not isinstance(doc, dict) or not doc:
        return ["expected a mapping with processors and/or connectors"]
    out: list[str] = []
    for section, comps in doc.items():
        if section not in KNOWN_COMPONENTS:
            out.append(f"unexpected top-level key {section!r}")
            continue
        for cid, conf in (comps or {}).items():
            ctype = str(cid).split("/", 1)[0]
            if ctype not in KNOWN_COMPONENTS[section]:
                out.append(f"unknown {section[:-1]} type {ctype!r}")
                continue
            if not isinstance(conf, dict):
                out.append(f"{cid}: settings must be a mapping")
                continue
            if ctype in ("filter", "transform") and conf.get("error_mode") not in (
                "ignore",
                "silent",
                "propagate",
            ):
                out.append(f"{cid}: error_mode must be ignore, silent or propagate")
            exprs: list[str] = []
            if ctype == "filter":
                keys = set(conf) & CONDITION_KEYS
                if len(keys) != 1:
                    out.append(f"{cid}: needs exactly one of {sorted(CONDITION_KEYS)}")
                for k in keys:
                    exprs += list(conf[k] or [])
            elif ctype == "transform":
                keys = set(conf) & STATEMENT_KEYS
                if len(keys) != 1:
                    out.append(f"{cid}: needs exactly one of {sorted(STATEMENT_KEYS)}")
                for k in keys:
                    for block in conf[k] or []:
                        if not {"context", "statements"} <= set(block):
                            out.append(f"{cid}: each {k} entry needs context and statements")
                        exprs += list(block.get("conditions") or []) + list(
                            block.get("statements") or []
                        )
            elif ctype == "count":
                for sig_type, metrics in conf.items():
                    if sig_type not in ("logs", "spans", "metrics", "datapoints"):
                        out.append(f"{cid}: unexpected key {sig_type!r}")
                        continue
                    for mname, mconf in (metrics or {}).items():
                        if not METRIC_NAME.match(str(mname)):
                            out.append(f"{cid}: metric name {mname!r} looks wrong")
                        exprs += list((mconf or {}).get("conditions") or [])
            if not exprs:
                out.append(f"{cid}: no conditions or statements")
            for e in exprs:
                out += _ottl_problems(str(e))
    return out


def full_collector_config(code: str) -> dict[str, Any]:
    """Wrap a snippet into a runnable config (otlp in, debug out) for `otelcol validate`."""
    doc = yaml.safe_load(code)
    processors = list((doc.get("processors") or {}).keys())
    connectors = list((doc.get("connectors") or {}).keys())
    signal = "logs"
    for p in processors:
        conf = doc["processors"][p]
        if "trace_conditions" in conf or "trace_statements" in conf:
            signal = "traces"
        if "metric_statements" in conf or "metric_conditions" in conf:
            signal = "metrics"
    pipelines: dict[str, Any] = {
        signal: {
            "receivers": ["otlp"],
            "processors": processors,
            "exporters": ["debug", *connectors],
        }
    }
    if connectors:
        pipelines["metrics/sift"] = {"receivers": connectors, "exporters": ["debug"]}
    return {
        "receivers": {"otlp": {"protocols": {"http": {"endpoint": "localhost:4318"}}}},
        "exporters": {"debug": {}},
        **{k: v for k, v in doc.items() if k in ("processors", "connectors")},
        "service": {"pipelines": pipelines},
    }


def otelcol_validate(code: str, binary: str | None) -> str:
    """'passed', 'failed: ...' or 'not run: ...'. Runs only when SIFT_OTELCOL_BIN points
    at a collector binary (`otelcol-contrib validate --config=<file>`)."""
    if not binary:
        return "not run: no collector binary (set SIFT_OTELCOL_BIN to run otelcol validate)"
    exe = shutil.which(binary) or binary
    if not Path(exe).exists():
        return f"not run: {binary} not found"
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(full_collector_config(code), f, sort_keys=False)
        path = f.name
    try:
        p = subprocess.run(
            [exe, "validate", f"--config={path}"], capture_output=True, text=True, timeout=60
        )
    finally:
        Path(path).unlink(missing_ok=True)
    if p.returncode == 0:
        return "passed"
    return "failed: " + (p.stderr or p.stdout).strip().splitlines()[-1][:300]
