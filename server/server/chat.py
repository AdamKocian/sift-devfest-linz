"""Ask Sift, fixture mode: a few scripted intents answered from engine numbers.

No Gemini call is made. Each answer lists the tool calls it is based on, and every number
in the text comes from one of those tool results. The live agent replaces this in phase 03;
it can create draft proposals but never applies them."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from server import engine, fmt, views
from server.engine import World
from server.models import Proposal, Signal

SERVICES = ("payment", "checkout", "cart", "inventory", "email", "frontend", "recommendation")
LEVELS = ("debug", "info", "warn", "error", "fatal")
STATUS_LABEL = {
    "awaiting_approval": "waiting for approval",
    "approved": "approved",
    "applied": "applied",
    "verifying": "being verified",
    "verified": "applied and verified",
    "reverted": "reverted",
    "rejected": "rejected",
}

SUGGESTIONS = [
    "Why is dropping payment DEBUG logs safe?",
    "What do checkout logs cost?",
    "Drop debug logs from payment but keep errors",
    "Can we drop the recommendation errors?",
    "Where is the biggest saving?",
]


@dataclass
class Answer:
    tools: list[dict[str, Any]] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def tool(
        self, tool: str, label: str, duration_ms: int, rows: int | None, result: dict[str, Any]
    ) -> dict[str, Any]:
        self.tools.append(
            {
                "tool": tool,
                "label": label,
                "duration_ms": duration_ms,
                "rows": rows,
                "result": result,
            }
        )
        return result

    def say(self, sentence: str) -> None:
        self.text.append(sentence)


def _service(msg: str) -> str | None:
    return next((s for s in SERVICES if re.search(rf"\b{s}\b", msg)), None)


def _level(msg: str) -> str | None:
    found = next((lv for lv in LEVELS if re.search(rf"\b{lv}\b", msg)), None)
    if found is None and re.search(r"\berrors?\b", msg):
        return "ERROR"
    return found.upper() if found else None


def _data_type(msg: str) -> str | None:
    if re.search(r"\blogs?\b", msg):
        return "logs"
    if re.search(r"\b(spans?|traces?)\b", msg):
        return "spans"
    if re.search(r"\bmetrics?\b", msg):
        return "metrics"
    return None


def _proposal_row(w: World, p: Proposal) -> dict[str, Any]:
    return views.proposal_summary(w, p)


# --- intents ----------------------------------------------------------------------------


def _explain_proposal(w: World, p: Proposal, a: Answer) -> None:
    sig = w.signals[p.signal_id]
    u = engine.usage(w, sig)
    d = engine.dry_run(w, p)
    s = engine.savings(w, p)
    a.tool(
        "get_usage",
        f"Read usage of {sig.service} · {sig.key}",
        500,
        rows=len(engine.affecting_refs(w, sig)),
        result={"references_30d": u.references_30d, "last_used": fmt.ago(u.last_used, w.now)},
    )
    a.tool(
        "validate_rule",
        "Re-ran the dry run",
        700,
        rows=round(d.matched_records_day),
        result={
            "matched": fmt.count(d.matched_records_day),
            "scope": fmt.count(d.scope_records_day),
            "errors_matched": fmt.count(d.errors_matched),
        },
    )
    a.tool(
        "estimate_savings",
        "Estimated savings at your rates",
        200,
        rows=1,
        result={"eur_month": fmt.eur(s.eur_month), "gib_month": fmt.gib(s.gib_month)},
    )
    ctx = engine.proposal_context(w, p)
    a.tool(
        "get_signals",
        f"Read volume of {sig.service} · {sig.key}",
        400,
        rows=1,
        result={k: v for k, v in ctx.items() if k not in ("service", "key")},
    )
    for template in p.explanation:
        a.say(re.sub(r"\s*\[\d\]", "", engine.render(template, ctx)))
    a.say(f"The proposal is {STATUS_LABEL[p.status]}.")
    a.proposals.append(_proposal_row(w, p))


def _explain_signal(w: World, sig: Signal, a: Answer) -> None:
    u = engine.usage(w, sig)
    current = engine.current_gib_day(w, sig)
    cost = engine.cost_month(w, sig, current)
    a.tool(
        "get_signals",
        f"Read {sig.service} · {sig.key}",
        400,
        rows=1,
        result={
            "gib_day": fmt.gib(current),
            "eur_month": fmt.eur(cost),
            "records_day": fmt.count(engine.records_day(sig, current)),
        },
    )
    a.tool(
        "get_usage",
        "Read its usage",
        500,
        rows=len(engine.affecting_refs(w, sig)),
        result={"references_30d": u.references_30d, "last_used": fmt.ago(u.last_used, w.now)},
    )
    a.say(
        f"{sig.service} · {sig.key} is {fmt.gib(current)} a day "
        f"({fmt.count(engine.records_day(sig, current))} records), about {fmt.eur(cost)} a "
        "month at your rates."
    )
    if u.references_30d:
        a.say(
            f"{u.references_30d} references read it in the last 30 days, the last one "
            f"{fmt.ago(u.last_used, w.now)}."
        )
    else:
        a.say(f"Nothing has read it in the last 30 days. Last read: {fmt.ago(u.last_used, w.now)}.")
    hits = engine.guardrail_hits(w, sig)
    if hits:
        a.say("It is protected: " + "; ".join(f"{g.name}. {g.reason}" for g in hits))
    if sig.finding_note:
        ctx = engine.signal_context(w, sig)
        a.tool(
            "get_patterns",
            "Read the finding",
            200,
            rows=1,
            result={k: v for k, v in ctx.items() if k not in ("service", "key")},
        )
        a.say(engine.render(sig.finding_note, ctx))
    p = engine.proposal_for(w, sig)
    if p is not None:
        a.say(f"There is a proposal for it: “{p.title}”, {STATUS_LABEL[p.status]}.")
        a.proposals.append(_proposal_row(w, p))


def _cost(w: World, service: str, data_type: str | None, a: Answer) -> None:
    sigs = [
        s
        for s in engine.partition(w)
        if s.service == service and (data_type is None or s.data_type == data_type)
    ]
    gib_day = sum(engine.current_gib_day(w, s) for s in sigs)
    eur = sum(engine.cost_month(w, s) for s in sigs)
    protected = [s for s in sigs if engine.guardrail_hits(w, s)]
    open_props = [
        p
        for p in w.proposals
        if p.status == "awaiting_approval"
        and w.signals[p.signal_id].service == service
        and (data_type is None or w.signals[p.signal_id].data_type == data_type)
    ]
    open_eur = sum(engine.savings(w, p).eur_month for p in open_props)
    what = {"logs": "logs", "spans": "spans", "metrics": "metrics", None: "telemetry"}[data_type]
    biggest = max(sigs, key=lambda s: engine.cost_month(w, s), default=None)
    a.tool(
        "get_signals",
        f"Read {service} {what}",
        600,
        rows=len(sigs),
        result={
            "gib_day": fmt.gib(gib_day),
            "eur_month": fmt.eur(eur),
            "signals": len(sigs),
            "protected": len(protected),
            "open_proposals": len(open_props),
            "open_eur_month": fmt.eur(open_eur),
            "largest": biggest.key if biggest else None,
            "largest_eur_month": fmt.eur(engine.cost_month(w, biggest)) if biggest else None,
        },
    )
    a.say(
        f"{service} ingests {fmt.gib(gib_day)} of {what} a day, about {fmt.eur(eur)} a month "
        "at your rates."
    )
    if biggest is not None and biggest in protected:
        refs = [r for r in engine.affecting_refs(w, biggest) if r.type in engine.STRONG_REF_TYPES]
        parts = [f"the {r.name} {r.type}" for r in refs[:3]]
        names = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        a.say(
            f"Most of it is {biggest.key} ({fmt.eur(engine.cost_month(w, biggest))} a month). "
            f"{names[:1].upper() + names[1:]} read it, so Sift protects it and will not "
            "propose dropping it."
        )
    if open_props:
        a.say(f"Open proposals for {service} would save {fmt.eur(open_eur)} a month.")
        a.proposals.extend(_proposal_row(w, p) for p in open_props)
    elif not protected:
        a.say(f"There are no open proposals for {service} {what}.")


def _find_signal(w: World, msg: str, service: str | None, level: str | None) -> Signal | None:
    keywords: list[tuple[str, str]] = [
        (r"health ?checks?|healthz|probes?", "sig-frontend-healthz"),
        (r"heartbeats?", "sig-inventory-heartbeat"),
        (r"stack ?traces?|duplicates?", "sig-cart-dup-trace"),
        (r"user\.?id", "sig-http-duration-userid"),
        (r"static", "sig-frontend-static"),
        (r"db\.statement|sql", "sig-inventory-dbstatement"),
    ]
    for pattern, sid in keywords:
        if re.search(pattern, msg) and sid in w.signals:
            return w.signals[sid]
    if service and level:
        return next(
            (
                s
                for s in w.signals.values()
                if s.service == service and s.kind == "log_source" and s.level == level
            ),
            None,
        )
    return None


def _instruction(w: World, msg: str, a: Answer) -> bool:
    service, level = _service(msg), _level(msg)
    sig = _find_signal(w, msg, service, level)
    if sig is None:
        return False
    hits = engine.guardrail_hits(w, sig)
    a.tool(
        "guardrails",
        f"Checked guardrails for {sig.service} · {sig.key}",
        300,
        rows=len(hits),
        result={"protected_by": [g.name for g in hits]},
    )
    if hits:
        cost = engine.cost_month(w, sig)
        a.tool(
            "estimate_savings",
            "Estimated what dropping it would save",
            200,
            rows=1,
            result={
                "eur_month": fmt.eur(cost),
                "gib_month": fmt.gib(engine.current_gib_day(w, sig) * 30),
            },
        )
        a.say(
            f"I won't propose that. {sig.service} · {sig.key} is protected by "
            + " and ".join(f"“{g.name}”" for g in hits)
            + f". {hits[0].reason}"
        )
        if sig.finding_note:
            ctx = engine.signal_context(w, sig)
            a.tool(
                "get_patterns",
                "Read the finding",
                200,
                rows=1,
                result={k: v for k, v in ctx.items() if k not in ("service", "key")},
            )
            a.say(engine.render(sig.finding_note, ctx))
        else:
            a.say(f"For reference, it costs about {fmt.eur(cost)} a month at your rates.")
        return True
    p = next(
        (
            p
            for p in w.proposals
            if p.signal_id == sig.id and p.status not in ("rejected", "reverted")
        ),
        None,
    )
    if p is not None:
        d = engine.dry_run(w, p)
        s = engine.savings(w, p)
        a.tool(
            "get_proposals",
            "Looked for an existing proposal",
            300,
            rows=1,
            result={"proposal": p.id, "status": p.status},
        )
        a.tool(
            "validate_rule",
            "Re-ran the dry run",
            700,
            rows=round(d.matched_records_day),
            result={
                "matched": fmt.count(d.matched_records_day),
                "scope": fmt.count(d.scope_records_day),
                "errors_matched": fmt.count(d.errors_matched),
                "eur_month": fmt.eur(s.eur_month),
            },
        )
        a.say(f"That is what “{p.title}” does, and it is {STATUS_LABEL[p.status]}.")
        if level == "DEBUG" or "keep" in msg:
            a.say("It matches DEBUG records only, so INFO, WARN and ERROR stay.")
        a.say(
            f"The dry run hits {fmt.count(d.matched_records_day)} of "
            f"{fmt.count(d.scope_records_day)} {d.scope_label} a day, "
            f"{fmt.count(d.errors_matched)} of them ERROR or FATAL. It saves about "
            f"{fmt.eur(s.eur_month)} a month."
        )
        if p.status == "awaiting_approval":
            a.say("Review the evidence and approve it below if it looks right.")
        a.proposals.append(_proposal_row(w, p))
        return True
    rejected = next((p for p in w.proposals if p.signal_id == sig.id), None)
    if rejected is not None:
        a.tool(
            "get_proposals",
            "Looked for an existing proposal",
            300,
            rows=1,
            result={"proposal": rejected.id, "status": rejected.status},
        )
        reason = rejected.reject_reason or rejected.revert_reason or "no reason given"
        a.say(
            f"This was proposed before as “{rejected.title}” and {STATUS_LABEL[rejected.status]}"
            f" ({reason}). I won't propose it again unless you change the guardrails."
        )
        a.proposals.append(_proposal_row(w, rejected))
        return True
    a.say(
        "In fixture mode I can only work with the recorded findings, so I can't draft a new "
        "rule for this yet. The live agent drafts it as a proposal for you to approve."
    )
    return True


def _ranking(w: World, a: Answer) -> None:
    awaiting = sorted(
        (p for p in w.proposals if p.status == "awaiting_approval"),
        key=lambda p: -engine.savings(w, p).eur_month,
    )
    total = sum(engine.savings(w, p).eur_month for p in awaiting)
    a.tool(
        "get_proposals",
        "Read open proposals",
        300,
        rows=len(awaiting),
        result={
            "awaiting": len(awaiting),
            "eur_month": fmt.eur(total),
            "top": [
                {"title": p.title, "eur_month": fmt.eur(engine.savings(w, p).eur_month)}
                for p in awaiting[:3]
            ],
        },
    )
    if not awaiting:
        a.say("Nothing is waiting for approval right now. Run an analysis to look again.")
        return
    top = awaiting[0]
    a.say(
        f"The biggest open saving is “{top.title}”: about "
        f"{fmt.eur(engine.savings(w, top).eur_month)} a month."
    )
    a.say(
        f"All {len(awaiting)} open proposals together would save about {fmt.eur(total)} "
        "a month at your rates."
    )
    a.proposals.extend(_proposal_row(w, p) for p in awaiting[:3])


def answer(
    w: World, message: str, proposal_id: str | None = None, signal_id: str | None = None
) -> Answer:
    msg = message.lower().strip()
    a = Answer()
    context_p = w.proposal(proposal_id) if proposal_id else None
    context_s = w.signals.get(signal_id) if signal_id else None

    intents: list[tuple[str, Callable[[], bool]]] = [
        (r"\bwhy\b.*\b(safe|drop|ok)\b|\bis (it|this) safe\b", lambda: _why(w, msg, context_p, a)),
        (r"\b(drop|remove|stop|filter|get rid of|delete)\b", lambda: _instruction(w, msg, a)),
        (r"\b(cost|costs|spend|pay|expensive|how much)\b", lambda: _cost_intent(w, msg, a)),
        (
            r"\b(biggest|most|top|largest|where)\b.*\b(sav|waste|money|cost)",
            lambda: (_ranking(w, a), True)[1],
        ),
        (
            r"\b(about|tell me|what do you know|explain)\b",
            lambda: _about(w, context_p, context_s, a),
        ),
    ]
    for pattern, handler in intents:
        if re.search(pattern, msg) and handler():
            return a
    if context_p is not None:
        _explain_proposal(w, context_p, a)
        return a
    if context_s is not None:
        _explain_signal(w, context_s, a)
        return a
    a.say(
        "Fixture mode replays a few scripted answers, so I can only help with questions like "
        "the ones below. The live agent on Gemini answers anything about your telemetry."
    )
    a.suggestions = list(SUGGESTIONS)
    return a


def _why(w: World, msg: str, context: Proposal | None, a: Answer) -> bool:
    p = context
    if p is None:
        service, level = _service(msg), _level(msg)
        sig = _find_signal(w, msg, service, level)
        p = next((x for x in w.proposals if sig and x.signal_id == sig.id), None)
    if p is None:
        return False
    _explain_proposal(w, p, a)
    return True


def _cost_intent(w: World, msg: str, a: Answer) -> bool:
    service = _service(msg)
    if service is None:
        return False
    _cost(w, service, _data_type(msg), a)
    return True


def _about(w: World, p: Proposal | None, s: Signal | None, a: Answer) -> bool:
    if p is not None:
        _explain_proposal(w, p, a)
        return True
    if s is not None:
        _explain_signal(w, s, a)
        return True
    return False


def events(
    w: World, message: str, proposal_id: str | None, signal_id: str | None
) -> list[dict[str, Any]]:
    """The SSE event sequence for one answer. The API adds pacing."""
    a = answer(w, message, proposal_id, signal_id)
    out: list[dict[str, Any]] = [{"type": "tool", **t} for t in a.tools]
    text = " ".join(a.text)
    for chunk in re.findall(r"\S+\s*", text):
        out.append({"type": "token", "text": chunk})
    out.extend({"type": "proposal", "proposal": p} for p in a.proposals)
    if a.suggestions:
        out.append({"type": "suggestions", "items": a.suggestions})
    out.append({"type": "done", "scripted": True})
    return out
