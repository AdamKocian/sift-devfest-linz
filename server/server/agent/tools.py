"""Sift's agent tools. The same code serves the live ADK agent, Ask Sift and the replay of a
recorded run, so a replay re-executes exactly what the real run did.

Numbers are computed here (engine + Grail dry runs), never by Gemini. Tool results carry
display-ready values ("€828", "11.5 GiB") so the agent can quote them, and the number
checker (checker.py) can find every number the agent writes.

Guardrails are enforced in `create_proposal`, not only in the prompt:
- ERROR/FATAL logs, security/audit logs, data an alert, SLO or workflow read in 90 days,
  and the custom protected list (engine.guardrail_hits) block removing actions;
- the dry run may not hit ERROR/FATAL records or records outside the signal;
- data a dashboard/notebook/workflow/alert/SLO read in 30 days is never removed, and data
  someone queried is never removed completely;
- no second proposal for the same signal, no re-proposing a rejected or reverted rule;
- savings must reach the user's threshold (except redaction);
- the explanation must render and every number in it must come from a tool result.
Each rejection goes back to the agent with its reason."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import Session

from server import dql, engine, fmt, services
from server.agent import checker, rules
from server.agent.rules import Draft, DraftError
from server.connectors.grail import GrailClient
from server.dql import DiscoverParams
from server.engines import discover, patterns
from server.engines.discover import GIB
from server.models import Proposal, Signal

FINDING_CLASSES = (
    "unused",
    "noise",
    "duplicate",
    "high_cardinality",
    "pii_risk",
    "over_verbose_level",
)
CONFIDENCE = ("high", "medium", "low")
MAX_SAMPLES = 6
PROPOSAL_PLACEHOLDERS = (
    "service",
    "key",
    "gib_day",
    "records_day",
    "gib_month",
    "eur_month",
    "last_used",
    "queries_30d",
    "save_gib_month",
    "save_gib_week",
    "save_eur_month",
    "matched",
    "scope_records",
    "scope_label",
    "match_pct",
    "share",
    "errors_matched",
)
SIGNAL_PLACEHOLDERS = PROPOSAL_PLACEHOLDERS[:8]


class ToolError(Exception):
    """A tool call that cannot be answered; the message goes back to the agent."""


def proposal_id(signal_id: str, action: str) -> str:
    base = re.sub(r"^(log|span|metric)-", "", signal_id)
    return f"p-{base}-{action.replace('_', '-')}"[:96]


def display_name(w: engine.World, sig: Signal) -> str:
    """'checkout INFO logs', 'frontend GET /healthz spans', 'user.id of a metric'."""
    if sig.kind == "log_source":
        return f"{sig.service} {sig.level} logs"
    if sig.kind == "log_pattern":
        return f"{sig.service} “{patterns.phrase(sig.key)}” logs"
    if sig.kind == "span":
        return f"{sig.service} {sig.key} spans"
    parent = w.signals.get(sig.parent_id or "")
    if parent is not None:
        return f"{sig.key} on {parent.key}"
    return sig.key


def _ago(dt: datetime | None, now: datetime) -> str:
    return fmt.ago(dt, now)


class SiftTools:
    NAMES = (
        "get_signals",
        "get_usage",
        "get_patterns",
        "record_finding",
        "draft_rule",
        "validate_rule",
        "estimate_savings",
        "create_proposal",
        "get_proposals",
        "apply_proposal",
    )
    INTERNAL = ("summarize_run",)  # called by code at the end of a run, not by the agent

    def __init__(
        self,
        session: Session,
        grail: GrailClient,
        clock: Callable[[], datetime],
        *,
        run_id: str | None = None,
        on_tool: Callable[[dict[str, Any]], None] | None = None,
        on_proposal: Callable[[Proposal], None] | None = None,
        otelcol_bin: str | None = None,
        user_message: str = "",
        world_time: datetime | None = None,
    ) -> None:
        self.session, self.grail, self.clock = session, grail, clock
        # "Now" for volumes, usage windows and "25 min ago": frozen at the start of the run,
        # so a replay sees exactly what the recorded run saw.
        self.world_time = world_time or clock()
        self.run_id = run_id
        self.on_tool, self.on_proposal = on_tool, on_proposal
        self.otelcol_bin = otelcol_bin
        self.user_message = user_message
        self.calls: list[dict[str, Any]] = []
        self.drafts: dict[str, Draft] = {}
        self.created: list[str] = []
        self.rejections: list[dict[str, Any]] = []
        self.findings: list[str] = []

    # --- plumbing ---------------------------------------------------------------------

    def world(self) -> engine.World:
        return engine.load_world(self.session, self.world_time)

    def sources(self) -> list[Any]:
        """What the number checker accepts: every tool result so far and the user's words."""
        return [c["result"] for c in self.calls] + [self.user_message]

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        t0 = time.perf_counter()
        result: dict[str, Any]
        if name not in self.NAMES and name not in self.INTERNAL:
            result = {"error": f"unknown tool {name}"}
        else:
            try:
                result = getattr(self, name)(**args)
            except (ToolError, DraftError) as e:
                result = {"error": str(e)}
            except TypeError as e:
                result = {"error": f"bad arguments: {e}"}
        ms = round((time.perf_counter() - t0) * 1000)
        label, rows = self._label(name, args, result)
        rec = {
            "name": name,
            "args": args,
            "result": result,
            "duration_ms": ms,
            "label": label,
            "rows": rows,
        }
        self.calls.append(rec)
        if self.on_tool:
            self.on_tool(rec)
        return result

    def _signal(self, w: engine.World, sid: str) -> Signal:
        sig = w.signals.get(sid)
        if sig is None:
            raise ToolError(f"no signal {sid!r}; call get_signals for ids")
        return sig

    def _draft(self, did: str) -> Draft:
        d = self.drafts.get(did)
        if d is None:
            raise ToolError(f"no draft {did!r} in this run; call draft_rule first")
        return d

    # --- reading ----------------------------------------------------------------------

    def _signal_row(self, w: engine.World, s: Signal) -> dict[str, Any]:
        u = engine.usage(w, s)
        cur = engine.current_gib_day(w, s)
        readers: dict[str, int] = dict(u.by_type)
        p = engine.proposal_for(w, s)
        row: dict[str, Any] = {
            "id": s.id,
            "kind": s.kind,
            "service": s.service,
            "key": s.key,
            "gib_day": fmt.gib(cur),
            "records_day": fmt.count(engine.records_day(s, cur)),
            "eur_month": fmt.eur(engine.cost_month(w, s, cur)),
            "usage": u.klass,
            "queries_30d": u.queries_30d,
        }
        if s.parent_id:
            row["parent"] = s.parent_id
        if s.level:
            row["level"] = s.level
        if s.source:
            row["source"] = s.source
        if readers:
            row["read_by_30d"] = readers
        hits = engine.guardrail_hits(w, s)
        if hits:
            row["protected_by"] = [g.name for g in hits]
        if p is not None:
            row["proposal"] = {"id": p.id, "status": p.status}
        kids = w.children.get(s.id)
        if kids:
            row["children"] = len(kids)
        return row

    def get_signals(self, kind: str = "", service: str = "", limit: int = 40) -> dict[str, Any]:
        w = self.world()
        sigs = [
            s
            for s in w.signals.values()
            if (not kind or s.kind == kind) and (not service or s.service == service)
        ]
        sigs.sort(key=lambda s: -engine.cost_month(w, s))
        top = engine.partition(w)
        total = sum(engine.cost_month(w, s) for s in top)
        total += sum(w.cost_model.monthly(k, v) for k, v in w.unattributed().items())
        rates = {k: f"€{v:.2f}" for k, v in w.settings["rates_eur_per_gib"].items()}
        return {
            "about": "Current ingest per signal, most expensive first. Costs are estimates at "
            "the user's own rates (not a Dynatrace price list). Children (patterns, "
            "dimensions, attributes) are parts of their parent.",
            "rates_eur_per_gib": rates,
            "min_savings_eur_month": fmt.eur(float(w.settings.get("min_savings_eur_month", 0))),
            "usage_window_days": 30,
            "guardrail_window_days": 90,
            "signals_total": len(w.signals),
            "eur_month_total": fmt.eur(total),
            "signals": [self._signal_row(w, s) for s in sigs[: max(1, min(limit, 60))]],
        }

    def _ref_row(self, r: Any, now: datetime) -> dict[str, Any]:
        out = {
            "type": r.type,
            "name": r.name,
            "owner": r.owner,
            "last_used": _ago(r.last_used, now),
        }
        if r.type == "query":
            out["count_30d"] = r.count_30d
        if r.fields:
            out["fields"] = r.fields
        if r.note:
            out["note"] = r.note
        return out

    def get_usage(self, signal_id: str) -> dict[str, Any]:
        w = self.world()
        sig = self._signal(w, signal_id)
        u = engine.usage(w, sig)
        reading = engine.affecting_refs(w, sig)
        inside: list[dict[str, Any]] = []
        for cid in engine.descendants(w, sig.id):
            child = w.signals[cid]
            for r in w.refs.get(cid, []):
                inside.append({"signal": child.id, **self._ref_row(r, w.now)})
        hits = engine.guardrail_hits(w, sig)
        out: dict[str, Any] = {
            "signal": sig.id,
            "window_days": 30,
            "usage": u.klass,
            "queries_30d": u.queries_30d,
            "last_used": _ago(u.last_used, w.now),
            "readers": [self._ref_row(r, w.now) for r in reading],
            "readers_of_parts": inside,
            "unaffected_neighbours": [
                {"type": r.type, "name": r.name} for r in engine.related_refs(w, sig)
            ],
            "guardrails": [{"name": g.name, "reason": g.reason} for g in hits],
        }
        unresolved = (w.settings.get("discovery") or {}).get("unresolved") or []
        if unresolved:
            out["unresolved_queries"] = len(unresolved)
        return out

    def get_patterns(self, signal_id: str) -> dict[str, Any]:
        w = self.world()
        sig = self._signal(w, signal_id)
        if sig.data_type != "logs":
            raise ToolError("patterns exist for logs only")
        src = w.signals[sig.parent_id] if sig.kind == "log_pattern" and sig.parent_id else sig
        src_gib = engine.current_gib_day(w, src) or 1e-12
        pats = []
        for cid in w.children.get(src.id, []):
            p = w.signals[cid]
            if p.kind != "log_pattern":
                continue
            g = engine.current_gib_day(w, p)
            pats.append(
                {
                    "id": p.id,
                    "template": p.key,
                    "phrase": patterns.phrase(p.key),
                    "share_of_source": fmt.pct(g / src_gib),
                    "gib_day": fmt.gib(g),
                    "records_day": fmt.count(engine.records_day(p, g)),
                    "eur_month": fmt.eur(engine.cost_month(w, p, g)),
                    "usage": engine.usage(w, p).klass,
                    "samples": list(p.samples or [])[:MAX_SAMPLES],
                }
            )
        return {
            "source": src.id,
            "level": src.level,
            "about": "Templates mined with Drain3 from a sample of the last 24 h. Samples are "
            "redacted. A pattern's phrase is what matchesPhrase() would match.",
            "patterns": pats,
            "source_samples": list(src.samples or [])[:MAX_SAMPLES],
        }

    def get_proposals(self, status: str = "") -> dict[str, Any]:
        w = self.world()
        items = []
        for p in sorted(w.proposals, key=lambda p: p.created_at, reverse=True):
            if status and p.status != status:
                continue
            s = engine.savings(w, p)
            row = {
                "id": p.id,
                "title": p.title,
                "signal_id": p.signal_id,
                "action": p.action,
                "status": p.status,
                "eur_month": fmt.eur(s.eur_month),
                "gib_month": fmt.gib(s.gib_month),
                "created": _ago(p.created_at, w.now),
            }
            if p.reject_reason:
                row["reject_reason"] = p.reject_reason
            if p.revert_reason:
                row["revert_reason"] = p.revert_reason
            items.append(row)
        listed = [p for p in w.proposals if not status or p.status == status]
        total = [engine.savings(w, p) for p in listed]
        return {
            "proposals": items[:30],
            "count": len(items),
            "total_saves_eur_month": fmt.eur(sum(x.eur_month for x in total)),
            "total_saves_gib_month": fmt.gib(sum(max(x.gib_month, 0.0) for x in total)),
            "created_this_run": len(self.created),
        }

    # --- findings ---------------------------------------------------------------------

    def _check_text(
        self, texts: list[str], ctx: dict[str, str], extra: list[Any]
    ) -> tuple[list[str], list[str]]:
        """Render templates; return (rendered, problems)."""
        rendered, problems = [], []
        for t in texts:
            try:
                rendered.append(engine.render(t, ctx))
            except KeyError as e:
                problems.append(f"{e.args[0]} in “{t[:60]}”; allowed: {sorted(ctx)}")
            except (ValueError, IndexError) as e:
                problems.append(f"bad template “{t[:60]}”: {e}")
        if not problems:
            missing = checker.unbacked(rendered, [*self.sources(), *extra])
            if missing:
                problems.append(
                    "these numbers do not appear in any tool result of this run: "
                    + ", ".join(missing)
                    + ". Use placeholders or numbers from tool results."
                )
        return rendered, problems

    def record_finding(self, signal_id: str, finding_class: str, note: str = "") -> dict[str, Any]:
        w = self.world()
        sig = self._signal(w, signal_id)
        if finding_class not in FINDING_CLASSES:
            raise ToolError(f"finding_class must be one of {list(FINDING_CLASSES)}")
        ctx = engine.signal_context(w, sig)
        rendered, problems = self._check_text([note] if note else [], ctx, [ctx])
        if problems:
            return {"recorded": False, "problems": problems}
        sig.finding_class, sig.finding_note = finding_class, (note or None)
        self.session.add(sig)
        self.session.commit()
        self.findings.append(sig.id)
        return {
            "recorded": True,
            "signal": sig.id,
            "finding_class": finding_class,
            "note": rendered[0] if rendered else None,
        }

    # --- rules ------------------------------------------------------------------------

    def draft_rule(
        self,
        signal_id: str,
        action: str,
        title: str = "",
        keep_phrase: str = "",
        only_status_code: int = 0,
        metric_name: str = "",
    ) -> dict[str, Any]:
        w = self.world()
        sig = self._signal(w, signal_id)
        did = f"d{len(self.drafts) + 1}"
        d = rules.build(
            w,
            sig,
            action,
            did,
            title=title,
            keep_phrase=keep_phrase.strip(),
            only_status_code=int(only_status_code or 0),
            metric_name=metric_name.strip(),
        )
        self.drafts[did] = d
        return d.summary()

    def _temp_proposal(self, d: Draft) -> Proposal:
        return Proposal(
            id=proposal_id(d.signal_id, d.action),
            signal_id=d.signal_id,
            title=d.title,
            action=d.action,
            finding_class="unused",
            confidence="medium",
            status="draft",
            reduction_ratio=d.reduction_ratio or 0.0,
            added_gib_day=d.added_gib_day,
            scope=d.scope,
            created_at=self.clock(),
        )

    def _placeholders(self, w: engine.World, d: Draft) -> dict[str, str]:
        return engine.proposal_context(w, self._temp_proposal(d))

    def validate_rule(self, draft_id: str) -> dict[str, Any]:
        d = self._draft(draft_id)
        w = self.world()
        sig = w.signals[d.signal_id]
        problems: list[str] = []
        otel_problems = rules.otel_structure_problems(d.otel["code"])
        otelcol = rules.otelcol_validate(d.otel["code"], self.otelcol_bin)
        problems += [f"OTel config: {p}" for p in otel_problems]
        if otelcol.startswith("failed"):
            problems.append(f"otelcol validate {otelcol}")
        params = DiscoverParams.from_settings(w.settings.get("discover"))
        dry: dict[str, Any]
        if sig.kind == "metric_dimension":
            parent = w.signals[sig.parent_id or ""]
            dims_q = dql.metric_dimensions(parent.key)
            dims = discover.dimension_keys(self.grail.query(dims_q).records)
            if sig.key not in dims:
                problems.append(f"{sig.key} is not a dimension of {parent.key}")
            card_q = dql.metric_cardinality(parent.key, dims)
            res = self.grail.query(card_q)
            row = res.records[0] if res.records else {}
            before = int(float(row.get("series") or 0))
            after = int(float(row.get(f"without_{dql.column(sig.key)}") or before))
            dry = {
                "query": card_q.dql,
                "window": "7 days",
                "series_now": fmt.count(before),
                "series_after": fmt.count(after),
                "series_removed": fmt.pct(1 - after / before) if before else "0 %",
                "scanned": f"{res.scanned_bytes / 1024**2:,.0f} MiB",
            }
            if after >= before:
                problems.append("removing the dimension would not reduce the series")
            d.reduction_ratio = 1.0
        else:
            table = "logs" if d.data_type == "logs" else "spans"
            field = sig.key if sig.kind == "span_attribute" else None
            meta = {
                "table": table,
                "scope": [asdict(c) for c in rules.scope_conds(sig)],
                "target": [asdict(c) for c in d.target],
                "matcher": [asdict(c) for c in d.matcher],
                "field": field,
            }
            q = dql.dry_run(
                f"dry_run:{d.signal_id}:{d.digest()}",
                table,
                rules.dql_and(rules.scope_conds(sig)),
                rules.dql_and(d.target),
                rules.dql_and(d.matcher),
                params.sampling_ratio,
                errors=table == "logs",
                field=field,
                meta=meta,
            )
            res = self.grail.query(q)
            row = res.records[0] if res.records else {}

            def n(k: str) -> float:
                return float(row.get(k) or 0)

            matched, target, scope = n("matched_records"), n("target_records"), n("scope_records")
            outside, errs = n("outside_target"), n("matched_errors")
            dry = {
                "query": q.dql,
                "window": "24 h",
                "matched_records": fmt.count(matched),
                "target_records": fmt.count(target),
                "scope_records": fmt.count(scope),
                "matched_share_of_target": fmt.pct(matched / target) if target else "0 %",
                "outside_target": fmt.count(outside),
                "scanned": f"{res.scanned_bytes / 1024**3:,.2f} GiB",
            }
            if table == "logs":
                dry["matched_errors"] = fmt.count(errs)
            if field:
                dry["with_field"] = fmt.count(n("with_field"))
            if matched <= 0:
                problems.append("the matcher hits no records in the last 24 h")
            if outside > 0:
                problems.append(
                    f"the matcher also hits {fmt.count(outside)} records outside {sig.id}"
                )
            if errs > 0 and d.action in engine.REMOVING_ACTIONS:
                problems.append(
                    f"the rule would remove {fmt.count(errs)} ERROR/FATAL records (guardrail)"
                )
            ratio = matched / target if target else 0.0
            if d.action == "redact":
                d.reduction_ratio = 0.0
            elif d.action == "drop_field":
                d.reduction_ratio = 1.0
            else:
                d.reduction_ratio = round(min(1.0, ratio), 4)
            if d.action == "logs_to_metric":
                s = discover.Sizing.from_settings(w.settings.get("discover"))
                series = 1  # dimension: service.name only
                d.added_gib_day = round(
                    series * s.metric_points_per_series_day * s.metric_bytes_per_point / GIB, 6
                )
        ok = not problems
        d.validation = {
            "ok": ok,
            "problems": problems,
            "dry_run": dry,
            "otel": {"structure": "ok" if not otel_problems else "failed", "otelcol": otelcol},
        }
        d.otel["note"] = (
            "Checked in code: YAML shape, component types and OTTL syntax"
            + (" passed" if not otel_problems else " failed")
            + f". otelcol validate: {otelcol}."
        )
        if d.action == "logs_to_metric":
            d.otel["note"] += " Wire the count connector into your pipelines as noted."
        out: dict[str, Any] = {
            "draft_id": d.id,
            "ok": ok,
            "problems": problems,
            "dry_run": {k: v for k, v in dry.items() if k != "query"},
            "otel_collector": d.validation["otel"],
        }
        if ok:
            out["reduction_of_signal"] = fmt.pct(d.reduction_ratio or 0.0)
            out["next"] = "estimate_savings(draft_id), then create_proposal"
        return out

    def estimate_savings(self, draft_id: str) -> dict[str, Any]:
        d = self._draft(draft_id)
        if not d.validation:
            raise ToolError("validate_rule first: savings use the measured dry run")
        w = self.world()
        p = self._temp_proposal(d)
        s = engine.savings(w, p)
        threshold = float(w.settings.get("min_savings_eur_month", 0))
        out: dict[str, Any] = {
            "draft_id": d.id,
            "saves_gib_month": fmt.gib(max(s.gib_month, 0.0)),
            "saves_eur_month": fmt.eur(max(s.eur_month, 0.0)),
            "at": "the user's own rates, all-in € per GiB (an estimate, not a Dynatrace price)",
            "threshold_eur_month": fmt.eur(threshold),
            "above_threshold": s.eur_month >= threshold or d.action == "redact",
            "placeholders_for_explanation": self._placeholders(w, d),
        }
        if d.added_gib_day:
            out["new_metric_gib_month"] = fmt.gib(d.added_gib_day * engine.DAYS_PER_MONTH)
        return out

    def _reject(self, d: Draft, reason: str, kind: str, rule: str = "") -> dict[str, Any]:
        self.rejections.append(
            {"draft_id": d.id, "signal_id": d.signal_id, "title": d.title, "kind": kind,
             "reason": reason, "rule": rule}
        )  # fmt: skip
        return {"created": False, "rejected_by": kind, "reason": reason}

    def create_proposal(
        self,
        draft_id: str,
        finding_class: str,
        confidence: str,
        explanation: list[str],
    ) -> dict[str, Any]:
        d = self._draft(draft_id)
        w = self.world()
        sig = w.signals[d.signal_id]
        if finding_class not in FINDING_CLASSES:
            raise ToolError(f"finding_class must be one of {list(FINDING_CLASSES)}")
        if confidence not in CONFIDENCE:
            raise ToolError(f"confidence must be one of {list(CONFIDENCE)}")

        # 1. guardrails, in code
        if d.action in engine.REMOVING_ACTIONS:
            affected = [sig, *(w.signals[c] for c in engine.descendants(w, sig.id))]
            for s in affected:
                hits = engine.guardrail_hits(w, s)
                if hits:
                    g = hits[0]
                    where = "" if s.id == sig.id else f" (part of it: {s.id})"
                    return self._reject(
                        d,
                        f"guardrail “{g.name}” protects {s.id}{where}: {g.reason}",
                        "guardrail",
                        g.name,
                    )
            # 2. usage: never remove data a dashboard, notebook, workflow, alert or SLO read
            #    in 30 days, and never remove all of something people query
            for s in affected:
                strong = [
                    r
                    for r in engine.affecting_refs(w, s)
                    if r.type in engine.STRONG_REF_TYPES
                    and r.last_used
                    and w.now - r.last_used <= timedelta(days=30)
                ]
                if strong:
                    names = ", ".join(f"{r.type} “{r.name}”" for r in strong[:3])
                    return self._reject(
                        d, f"{s.id} is read by {names} in the last 30 days", "usage"
                    )
            queries = sum(
                r.count_30d for s in affected for r in engine.affecting_refs(w, s)
                if r.type == "query"
            )  # fmt: skip
            if queries and (d.reduction_ratio or 0) >= 0.999:
                return self._reject(
                    d,
                    f"people queried this data {queries} times in 30 days; a rule may remove "
                    "duplicates or part of it, not all of it",
                    "usage",
                )
        # 3. validated
        if not d.validation:
            return self._reject(d, "validate_rule first", "validation")
        if not d.validation["ok"]:
            return self._reject(
                d, "validation failed: " + "; ".join(d.validation["problems"]), "validation"
            )
        # 4. one proposal per signal and action; respect earlier decisions
        pid = proposal_id(d.signal_id, d.action)
        for p in w.proposals:
            if p.signal_id != d.signal_id:
                continue
            if p.status == "rejected":
                return self._reject(
                    d,
                    f"“{p.title}” was rejected by {p.decided_by}: "
                    f"{p.reject_reason or 'no reason given'}. Not proposing it again.",
                    "history",
                )
            if p.status == "reverted":
                return self._reject(
                    d,
                    f"“{p.title}” was reverted: {p.revert_reason or 'verification failed'}",
                    "history",
                )
            return self._reject(
                d, f"“{p.title}” already exists for this signal ({p.status})", "duplicate"
            )
        if self.session.get(Proposal, pid) is not None:
            return self._reject(d, f"proposal {pid} already exists", "duplicate")
        # 5. worth it
        temp = self._temp_proposal(d)
        s = engine.savings(w, temp)
        threshold = float(w.settings.get("min_savings_eur_month", 0))
        if d.action != "redact" and s.eur_month < threshold:
            return self._reject(
                d,
                f"saves {fmt.eur(s.eur_month)} a month, below the {fmt.eur(threshold)} threshold",
                "threshold",
            )
        # 6. the explanation: 3-5 sentences, placeholders known, every number sourced
        if not isinstance(explanation, list) or not 3 <= len(explanation) <= 5:
            return self._reject(d, "explanation must be a list of 3 to 5 sentences", "text")
        ctx = self._placeholders(w, d)
        rendered, problems = self._check_text([str(t) for t in explanation], ctx, [ctx])
        if problems:
            return self._reject(d, "explanation: " + " ".join(problems), "text")

        definition = {
            "id": pid,
            "signal_id": d.signal_id,
            "title": d.title,
            "action": d.action,
            "finding_class": finding_class,
            "confidence": confidence,
            "reduction_ratio": d.reduction_ratio or 0.0,
            "added_gib_day": d.added_gib_day,
            "scope": d.scope,
            "explanation": [str(t) for t in explanation],
            "rule": {
                "openpipeline": d.openpipeline,
                "otel": d.otel,
                "validation": d.validation,
                "params": d.params,
            },
        }
        p = services.create_proposal(self.session, definition, self.clock(), run_id=self.run_id)
        self.created.append(p.id)
        if self.on_proposal:
            self.on_proposal(p)
        return {
            "created": True,
            "proposal_id": p.id,
            "title": p.title,
            "status": p.status,
            "saves_eur_month": fmt.eur(s.eur_month),
            "explanation_rendered": rendered,
        }

    def summarize_run(self) -> dict[str, Any]:
        """What this run changed, computed by code; the run summary quotes only this."""
        w = self.world()
        created = [p for p in w.proposals if p.id in self.created]
        awaiting = [p for p in w.proposals if p.status == "awaiting_approval"]

        def eur(ps: list[Proposal]) -> str:
            return fmt.eur(sum(engine.savings(w, p).eur_month for p in ps))

        by_kind: dict[str, list[str]] = {}
        for r in self.rejections:
            what = f"{r['title']} ({r['rule']})" if r["rule"] else r["title"]
            by_kind.setdefault(r["kind"], []).append(what)
        touched = {p.signal_id for p in w.proposals}
        threshold = float(w.settings.get("min_savings_eur_month", 0))
        protected = []
        for sig in sorted(engine.partition(w), key=lambda x: -engine.cost_month(w, x)):
            hits = engine.guardrail_hits(w, sig)
            cost = engine.cost_month(w, sig)
            parts = {sig.id, *engine.descendants(w, sig.id)}
            if hits and cost >= threshold and not parts & touched:
                protected.append(
                    {"signal": sig.id, "name": display_name(w, sig), "eur_month": fmt.eur(cost),
                     "guardrails": [g.name for g in hits]}
                )  # fmt: skip
        return {
            "created": len(created),
            "created_titles": [p.title for p in created],
            "created_saves_eur_month": eur(created),
            "awaiting_approval": len(awaiting),
            "awaiting_saves_eur_month": eur(awaiting),
            "stopped_by_guardrails": by_kind.get("guardrail", []),
            "left_alone_in_use": by_kind.get("usage", []),
            "already_proposed_or_decided": len(by_kind.get("duplicate", []))
            + len(by_kind.get("history", [])),
            "other_rejections": sum(
                len(v) for k, v in by_kind.items()
                if k not in ("guardrail", "usage", "duplicate", "history")
            ),
            "findings_recorded": len(self.findings),
            "protected_left_alone": protected,
        }  # fmt: skip

    def apply_proposal(self, proposal_id: str) -> dict[str, Any]:
        p = self.session.get(Proposal, proposal_id)
        if p is None:
            raise ToolError(f"no proposal {proposal_id!r}")
        return {
            "applied": False,
            "status": p.status,
            "message": "Sift never applies a rule by itself. A person reviews the evidence and "
            "approves it in the Sift UI (Apply rule); until then it is awaiting approval.",
        }

    # --- run log labels ---------------------------------------------------------------

    def _label(self, name: str, args: dict[str, Any], r: dict[str, Any]) -> tuple[str, int | None]:
        if "error" in r:
            return f"{name}: {r['error']}", None
        sid = str(args.get("signal_id", ""))
        if name == "get_signals":
            return f"Read {len(r['signals'])} signals by cost (DQL volume)", len(r["signals"])
        if name == "get_usage":
            n = len(r["readers"]) + len(r["readers_of_parts"])
            return f"Read usage of {sid}: {r['usage']}, {n} references", n
        if name == "get_patterns":
            return f"Read {len(r['patterns'])} log patterns of {r['source']}", len(r["patterns"])
        if name == "record_finding":
            if not r.get("recorded"):
                return f"Finding for {sid} not recorded: {'; '.join(r['problems'])}", None
            note = f": {r['note']}" if r.get("note") else ""
            return f"Finding {r['finding_class']} for {sid}{note}", 1
        if name == "draft_rule":
            return f"Drafted “{r['title']}” ({r['action']})", 1
        if name == "validate_rule":
            dr = r["dry_run"]
            if "matched_records" in dr:
                head = (
                    f"Dry run ({dr['window']}): hits {dr['matched_records']} of "
                    f"{dr['scope_records']} records"
                )
            else:
                head = f"Dry run ({dr['window']}): {dr['series_now']} → {dr['series_after']} series"
            tail = " · passed" if r["ok"] else " · failed: " + "; ".join(r["problems"])
            return head + tail, None
        if name == "estimate_savings":
            return f"Saves about {r['saves_eur_month']} a month ({r['saves_gib_month']})", 1
        if name == "create_proposal":
            if r.get("created"):
                return f"Created proposal “{r['title']}”", 1
            d = self.drafts.get(str(args.get("draft_id")))
            what = f"“{d.title}”" if d else str(args.get("draft_id"))
            kind = {"guardrail": "Blocked by guardrail", "usage": "Not proposed (in use)"}.get(
                r.get("rejected_by", ""), "Not proposed"
            )
            return f"{kind}: {what}. {r['reason']}", 0
        if name == "get_proposals":
            return f"Read {r['count']} proposals", r["count"]
        if name == "summarize_run":
            names = "; ".join(
                f"{x['name']} ({', '.join(x['guardrails'])})" for x in r["protected_left_alone"]
            )
            return (
                f"Summed up the run: {r['created']} new proposals, "
                f"{r['awaiting_approval']} awaiting approval. Protected by guardrails and left "
                f"alone: {names or 'none'}"
            ), r["created"]
        if name == "apply_proposal":
            return f"Apply needs a person: {args.get('proposal_id')} is {r['status']}", None
        return name, None
