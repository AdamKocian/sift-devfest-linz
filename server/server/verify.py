"""Verification of an applied rule, after the verification window W.

Three checks, each a query:
1. Volume: the affected signal before vs after the rule, in one scan over the last 2·W
   minutes (`dql.verify_volume` / `dql.verify_metric_dimension`). Passes when the observed
   drop is at least 70 % of the predicted one. A redact rule must not drop anything: the
   records keep arriving and none shows an unmasked address.
2. References: every dashboard tile, notebook section, workflow, alert and SLO query next to
   the data is re-run over the last W minutes and must still return data.
3. Davis problems: no problem started since the rule was applied. Asked through the
   Dynatrace MCP server (`execute_dql`), with Grail as the fallback when MCP is unavailable.

Where the answers come from (`Evidence`):
- `LiveEvidence`: Grail + the Dynatrace MCP server (SIFT_MODE=live; not run yet, no tenant).
- `RecordedEvidence`: fixtures/verify/<proposal id>.json, answers recorded for the exact
  query texts (checked) at the recording's window. Fixture mode, no network.
- `DerivedEvidence`: fixture mode for a proposal without a recording (one a live agent run
  created): answers derived from the demo world, labelled as such.

On failure the caller reverts the rule from its snapshot and stores an incident note:
recorded (fixture), written by Gemini (live with SIFT_AGENT=gemini, number-checked), or the
template below."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from server import dql, engine, fmt
from server.agent import rules
from server.agent.checker import unbacked
from server.connectors.base import ConnectorError
from server.connectors.grail import GrailClient, Query
from server.connectors.mcp_client import DynatraceMcp, query_argument, result_records
from server.engines.dql_parser import Variable, substitute
from server.models import Proposal, Reference, Signal

PASS_SHARE = 0.7


@dataclass
class RefCheck:
    label: str
    key: str  # type:name, stable across seeds (reference ids are not)
    query: Query | None  # None: cannot be re-run on its own (dashboard variables)


@dataclass
class Plan:
    window_min: int
    volume: Query
    kind: str  # records | presence | series | redact
    predicted: float  # share of the measured records the rule should remove
    refs: list[RefCheck]
    problems: Query


def ref_label(r: Reference) -> str:
    return f"{r.type.capitalize()} “{r.name}”"


def checked_refs(w: engine.World, p: Proposal) -> list[Reference]:
    """Strong references that read the data, then the ones next to it, once each."""
    sig = w.signals[p.signal_id]
    refs = [r for r in engine.affecting_refs(w, sig) if r.type in engine.STRONG_REF_TYPES]
    refs += engine.related_refs(w, sig)
    out: list[Reference] = []
    seen: set[str] = set()
    for r in refs:
        key = f"{r.type}:{r.name}"
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


_VAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def rerun_text(sig: Signal, r: Reference) -> str | None:
    """The reference's query as it can run on its own, or None.

    Dashboard variables have no value outside the dashboard. When the rule removes an
    attribute or dimension that the reference reads through a variable (a "group by
    $Attribute" tile), the variable is set to that field: the case the rule could break.
    Any other variable: not re-run (the check says so)."""
    if not r.dql:
        return None
    names = set(_VAR.findall(r.dql))
    if not names:
        return r.dql
    if sig.kind not in ("span_attribute", "metric_dimension") or sig.key not in r.fields:
        return None
    text, missing = substitute(r.dql, {n: Variable(n, (sig.key,)) for n in names})
    return None if missing else text


def plan(w: engine.World, p: Proposal, window_min: int) -> Plan:
    sig = w.signals[p.signal_id]
    name = f"verify_volume:{p.id}"
    if sig.kind in ("log_source", "log_pattern", "span"):
        conds = rules.dql_and(rules.target_conds(w, sig))
        redact = p.action == "redact"
        q = dql.verify_volume(name, sig.data_type, conds, window_min, unmasked=redact)
        kind, predicted = ("redact", 0.0) if redact else ("records", p.reduction_ratio)
    elif sig.kind == "span_attribute":
        conds = rules.dql_and(rules.target_conds(w, sig))
        q = dql.verify_volume(name, "spans", conds, window_min, present=sig.key)
        kind, predicted = "presence", 1.0
    elif sig.kind == "metric_dimension":
        parent = w.signals[sig.parent_id or ""]
        q = dql.verify_metric_dimension(name, parent.key, sig.key, window_min)
        kind, predicted = "series", 1.0
    else:
        raise ValueError(f"no verification query for a {sig.kind}")
    refs = []
    for r in checked_refs(w, p):
        key = f"{r.type}:{r.name}"
        text = rerun_text(sig, r)
        rq = None
        if text is not None:
            rq = Query(f"verify_ref:{key}", text, max_records=1, timeframe_min=window_min)
        refs.append(RefCheck(ref_label(r), key, rq))
    return Plan(window_min, q, kind, predicted, refs, dql.davis_problems(window_min))


# --- evidence -------------------------------------------------------------------------


class Evidence(Protocol):
    source: str  # live | recorded | derived

    def volume(self, q: Query) -> dict[str, Any]: ...

    def reference(self, key: str, q: Query) -> int: ...

    def problems(self, q: Query) -> tuple[list[dict[str, Any]], str]: ...


class LiveEvidence:
    source = "live"

    def __init__(self, grail: GrailClient, mcp: DynatraceMcp) -> None:
        self.grail, self.mcp = grail, mcp

    def volume(self, q: Query) -> dict[str, Any]:
        records = self.grail.query(q).records
        return records[0] if records else {"before": 0, "after": 0}

    def reference(self, key: str, q: Query) -> int:
        return len(self.grail.query(q).records)

    def problems(self, q: Query) -> tuple[list[dict[str, Any]], str]:
        try:
            tool = next((t for t in self.mcp.list_tools() if t.name == "execute_dql"), None)
            if tool is None:
                raise ConnectorError("the Dynatrace MCP server lists no execute_dql tool")
            result = self.mcp.call_tool(tool.name, {query_argument(tool): q.dql})
            return result_records(result), "Dynatrace MCP execute_dql"
        except ConnectorError as e:
            return self.grail.query(q).records, f"Grail (MCP unavailable: {e})"


class RecordedEvidence:
    """Answers recorded for the exact queries this code builds (texts are compared)."""

    source = "recorded"

    def __init__(self, rec: dict[str, Any], path: Path) -> None:
        self.rec, self.path = rec, path

    def _check(self, q: Query, recorded: str) -> None:
        if recorded.strip() != q.dql.strip():
            raise ConnectorError(
                f"{self.path.name}: recorded query for {q.name} differs from the current one; "
                "re-record with `make fixtures-synth`"
            )

    def volume(self, q: Query) -> dict[str, Any]:
        v = self.rec["volume"]
        self._check(q, v["query"])
        return v["records"][0]

    def reference(self, key: str, q: Query) -> int:
        r = self.rec["references"].get(key)
        if r is None:
            raise ConnectorError(f"{self.path.name}: no recorded answer for {key}")
        self._check(q, r["query"])
        return int(r["records"])

    def problems(self, q: Query) -> tuple[list[dict[str, Any]], str]:
        pr = self.rec["problems"]
        self._check(q, pr["query"])
        return result_records(pr["result"]), f"Dynatrace MCP {pr['tool']} (recorded)"

    @property
    def note(self) -> str | None:
        return self.rec.get("incident_note")


class DerivedEvidence:
    """Fixture mode without a recording: what the demo world says the rule does."""

    source = "derived"

    def __init__(self, w: engine.World, p: Proposal, pl: Plan) -> None:
        self.w, self.p, self.pl = w, p, pl

    def volume(self, q: Query) -> dict[str, Any]:
        sig = self.w.signals[self.p.signal_id]
        if self.pl.kind == "series":
            before = 40.0
        else:
            before = max(engine.records_day(sig) * self.pl.window_min / 1440, 1.0)
        after = before * (1 - self.pl.predicted * 0.98)
        out: dict[str, Any] = {"before": round(before), "after": round(after)}
        if self.pl.kind == "redact":
            out["unmasked_after"] = 0
        return out

    def reference(self, key: str, q: Query) -> int:
        return 1

    def problems(self, q: Query) -> tuple[list[dict[str, Any]], str]:
        return [], "derived from the demo world"


class FailingEvidence:
    """Settings > Verification > "Fail the next verification" (fixture mode): the first
    reference stops returning data, or, with no references, the volume does not drop."""

    def __init__(self, inner: Evidence, pl: Plan) -> None:
        self.inner, self.pl = inner, pl
        self.source = inner.source
        self.target = next((r.key for r in pl.refs if r.query), None)

    def volume(self, q: Query) -> dict[str, Any]:
        v = dict(self.inner.volume(q))
        if self.target is None:
            v["after"] = v["before"]
        return v

    def reference(self, key: str, q: Query) -> int:
        return 0 if key == self.target else self.inner.reference(key, q)

    def problems(self, q: Query) -> tuple[list[dict[str, Any]], str]:
        return self.inner.problems(q)


def recording(fixtures_dir: Path, pid: str) -> RecordedEvidence | None:
    path = fixtures_dir / "verify" / f"{pid}.json"
    if not path.exists():
        return None
    return RecordedEvidence(json.loads(path.read_text()), path)


# --- checks -----------------------------------------------------------------------------


def _volume_check(w: engine.World, p: Proposal, pl: Plan, ev: Evidence) -> dict[str, Any]:
    rec = ev.volume(pl.volume)
    before, after = float(rec.get("before") or 0), float(rec.get("after") or 0)
    base = {
        "name": "volume",
        "query": pl.volume.dql,
        "observed": {"before": before, "after": after},
    }
    if pl.kind == "redact":
        unmasked = int(rec.get("unmasked_after") or 0)
        ok = before > 0 and after >= PASS_SHARE * before and unmasked == 0
        if before == 0:
            detail = "No matching records before the rule, so nothing could be compared"
        elif unmasked:
            detail = f"{fmt.count(unmasked)} records after the rule still show an address"
        elif not ok:
            detail = f"Matching records fell from {fmt.count(before)} to {fmt.count(after)}"
        else:
            detail = "Matching records still arrive, with the address masked"
        return {**base, "label": "Records still arrive, masked", "passed": ok, "detail": detail}
    observed = 1 - after / before if before else 0.0
    ok = before > 0 and observed >= PASS_SHARE * pl.predicted
    s = engine.savings(w, p)
    share = observed / pl.predicted if pl.predicted else 0.0
    unit = {"series": "series with the dimension", "presence": "spans with the attribute"}.get(
        pl.kind, "matching records"
    )
    if before == 0:
        detail = f"No {unit} in the {pl.window_min} min before the rule; nothing to compare"
    else:
        detail = (
            f"−{fmt.gib(s.gib_day * share)} a day (predicted −{fmt.gib(s.gib_day)}): "
            f"{unit} fell from {fmt.count(before)} to {fmt.count(after)}, "
            f"{fmt.pct(share)} of the predicted drop"
        )
    return {**base, "label": "Volume changed as predicted", "passed": ok, "detail": detail}


def _references_check(pl: Plan, ev: Evidence) -> dict[str, Any]:
    results = []
    for r in pl.refs:
        n = None if r.query is None else ev.reference(r.key, r.query)
        results.append({"ref": r.label, "records": n, "query": r.query.dql if r.query else None})
    run = [x for x in results if x["records"] is not None]
    empty = [x for x in run if x["records"] == 0]
    skipped = len(results) - len(run)
    if not results:
        detail = "Nothing references this data"
    elif empty:
        detail = f"{empty[0]['ref']} returned no data after the rule was applied."
    else:
        detail = f"{len(run)} of {len(run)} still return data"
        if skipped:
            detail += f" ({skipped} use dashboard variables and were not re-run)"
    return {
        "name": "references",
        "label": "Queries, dashboards and alerts still return data",
        "passed": not empty,
        "detail": detail,
        "results": results,
    }


def _problems_check(pl: Plan, ev: Evidence) -> dict[str, Any]:
    try:
        problems, via = ev.problems(pl.problems)
    except ConnectorError as e:
        return {
            "name": "problems",
            "label": "No new Davis problems",
            "passed": False,
            "detail": f"Could not read Davis problems: {e}",
            "query": pl.problems.dql,
        }
    if problems:
        first = problems[0]
        name = first.get("event.name") or "unnamed problem"
        detail = f"New problem {first.get('display_id', '?')}: {name}"
        if len(problems) > 1:
            detail += f" (+{len(problems) - 1} more)"
    else:
        detail = "0 new problems"
    return {
        "name": "problems",
        "label": "No new Davis problems",
        "passed": not problems,
        "detail": detail,
        "via": via,
        "query": pl.problems.dql,
        "problems": problems[:10],
    }


def run_checks(w: engine.World, p: Proposal, pl: Plan, ev: Evidence) -> list[dict[str, Any]]:
    return [_volume_check(w, p, pl, ev), _references_check(pl, ev), _problems_check(pl, ev)]


# --- incident note ----------------------------------------------------------------------

NEXT_STEP = {
    "volume": "Compare the matcher with sample records (Rule tab, dry run) before proposing "
    "it again.",
    "references": "Ask the owner whether it still needs this data, then narrow the rule or "
    "protect the data with a guardrail.",
    "problems": "Look at the problem first; propose the rule again only once it is closed "
    "and unrelated.",
}


@dataclass
class Incident:
    text: str
    by: str  # agent (recorded) | agent | sift
    facts: dict[str, Any] = field(default_factory=dict)


def incident_facts(p: Proposal, checks: list[dict[str, Any]], minutes: int) -> dict[str, Any]:
    return {
        "title": p.title,
        "minutes_after_apply": minutes,
        "checks": [{k: c[k] for k in ("name", "label", "passed", "detail")} for c in checks],
    }


def template_note(facts: dict[str, Any]) -> str:
    failed = [c for c in facts["checks"] if not c["passed"]]
    first = failed[0]
    passed = [c["label"].lower() for c in facts["checks"] if c["passed"]]
    evidence = f" The other checks passed: {', '.join(passed)}." if passed else ""
    return (
        f"“{facts['title']}” was reverted {facts['minutes_after_apply']} min after it was "
        f"applied. {first['detail'].rstrip('.')}.{evidence} Sift restored the previous state. "
        f"Next step: {NEXT_STEP[first['name']]}"
    )


def incident_note(
    p: Proposal,
    checks: list[dict[str, Any]],
    minutes: int,
    *,
    recorded: str | None = None,
    writer: Callable[[dict[str, Any]], str] | None = None,
) -> Incident:
    facts = incident_facts(p, checks, minutes)
    if recorded:
        return Incident(recorded, "agent (recorded)", facts)
    if writer is not None:
        try:
            text = writer(facts).strip()
        except Exception:  # noqa: BLE001  (a failed note must not stop the revert)
            text = ""
        # Every number in the note must come from the check results (same rule as runs).
        if text and not unbacked([text], [facts]):
            return Incident(text, "agent", facts)
    return Incident(template_note(facts), "sift", facts)
