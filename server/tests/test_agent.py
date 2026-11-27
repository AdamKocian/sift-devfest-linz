"""The agent's tools on the fixture world: guardrails and checks in code, rules for both
targets, the dry run through the fixture GrailClient, and the number checker."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlmodel import Session

from server import engine, fixtures
from server.agent import checker, rules
from server.agent.tools import SiftTools, proposal_id
from server.config import Config, load_config
from server.connectors import fixture_connectors
from server.connectors.base import FixtureMissingError
from server.db import make_engine, utcnow
from server.synth import dry_run_recorder

EXPLAIN = [
    "{service} writes {records_day} records a day, {share} of {scope_label} [1].",
    "Nobody read them in the last 30 days [2].",
    "The rule saves about {save_gib_month} and {save_eur_month} a month [3].",
]


@pytest.fixture
def clean(tmp_path: Path) -> Iterator[tuple[Session, SiftTools]]:
    """The state the analyze run was recorded in: discovery only, no history."""
    cfg: Config = dataclasses.replace(load_config(), db_path=tmp_path / "sift.db")
    db = make_engine(cfg.db_path)
    now = utcnow()
    fixtures.seed(db, cfg, now, history=False)
    conn = fixture_connectors(cfg.fixtures_dir / "dynatrace", now)
    conn.grail.on_missing = dry_run_recorder(tmp_path / "dyn").query  # type: ignore[attr-defined]
    with Session(db, expire_on_commit=False) as s:
        yield s, SiftTools(s, conn.grail, utcnow, run_id="run_t", world_time=now)


def propose(t: SiftTools, sid: str, action: str, **kw: object) -> dict:
    t.call("get_usage", {"signal_id": sid})  # the agent reads the evidence first
    d = t.call("draft_rule", {"signal_id": sid, "action": action, **kw})
    assert "error" not in d, d
    t.call("validate_rule", {"draft_id": d["draft_id"]})
    t.call("estimate_savings", {"draft_id": d["draft_id"]})
    return t.call(
        "create_proposal",
        {
            "draft_id": d["draft_id"],
            "finding_class": "unused",
            "confidence": "high",
            "explanation": EXPLAIN,
        },
    )


CASES = [  # DEMO_WORLD case, signal, action, options, expected reduction of the signal
    (1, "log-payment-app-debug", "drop", {}, 1.0),
    (2, "span-frontend-get-healthz", "drop", {"only_status_code": 200}, 1.0),
    (
        3,
        "log-cart-app-warn-timeout-calling-inventory-65c2",
        "drop",
        {"keep_phrase": "InventoryClient.reserve"},
        0.6667,
    ),
    (
        4,
        "log-inventory-app-info-heartbeat-ok-worker-8730",
        "logs_to_metric",
        {"metric_name": "inventory.heartbeat.count"},
        1.0,
    ),
    (5, "metric-frontend-http-server-request-duration-dim-user-id", "drop_field", {}, 1.0),
    (6, "log-email-app-info-sent-confirmation-to-1f3c", "redact", {}, 0.0),
]


@pytest.mark.parametrize(("case", "sid", "action", "kw", "ratio"), CASES)
def test_cases_1_to_6_pass_every_check(
    clean: tuple[Session, SiftTools], case: int, sid: str, action: str, kw: dict, ratio: float
) -> None:
    s, t = clean
    r = propose(t, sid, action, **kw)
    assert r["created"], r
    w = engine.load_world(s, utcnow())
    p = w.proposal(proposal_id(sid, action))
    assert p is not None and p.status == "awaiting_approval" and p.action == action
    assert p.reduction_ratio == pytest.approx(ratio, abs=1e-3)
    # the measured dry run and the OTel check travel with the rule
    v = p.rule["validation"]
    assert v["ok"] and v["otel"]["structure"] == "ok"
    assert v["otel"]["otelcol"].startswith("not run")
    assert v["dry_run"]["query"].startswith(("fetch ", "metrics"))
    assert "otelcol validate: not run" in p.rule["otel"]["note"]


def test_case_7_error_logs_are_blocked_by_the_guardrail(clean: tuple[Session, SiftTools]) -> None:
    _, t = clean
    r = propose(t, "log-recommendation-app-error", "drop")
    assert not r["created"] and r["rejected_by"] == "guardrail"
    assert "ERROR and FATAL logs" in r["reason"]
    # the pattern inside it too, and the dry run itself refuses to hit ERROR records
    d = t.call(
        "draft_rule",
        {
            "signal_id": "log-recommendation-app-error-"
            "modelweightsexpiredexception-weights-older-e4d4",
            "action": "drop",
        },
    )
    v = t.call("validate_rule", {"draft_id": d["draft_id"]})
    assert not v["ok"] and any("ERROR/FATAL" in p for p in v["problems"])
    assert t.created == []


def test_case_8_data_an_alert_reads_is_blocked(clean: tuple[Session, SiftTools]) -> None:
    _, t = clean
    r = propose(t, "log-checkout-app-info", "drop")
    assert not r["created"] and r["rejected_by"] == "guardrail"
    assert "alert, SLO or workflow" in r["reason"]


def test_audit_logs_and_the_protected_list_are_blocked(clean: tuple[Session, SiftTools]) -> None:
    s, t = clean
    r = propose(t, "log-checkout-audit-info", "drop")
    assert r["rejected_by"] == "guardrail"
    from server import services

    services.add_guardrail(
        s,
        {"name": "frontend static", "reason": "CDN debugging", "service": "frontend",
         "kind": "span", "key_pattern": "GET /static/*"},
        "Lena Hofer",
        utcnow(),
    )  # fmt: skip
    r = propose(t, "span-frontend-get-static", "drop")
    assert r["rejected_by"] == "guardrail" and "frontend static" in r["reason"]


def test_used_data_is_redacted_not_dropped(clean: tuple[Session, SiftTools]) -> None:
    _, t = clean
    r = propose(t, "log-email-app-info-sent-confirmation-to-1f3c", "drop")
    assert r["rejected_by"] == "usage" and "Email delivery audit" in r["reason"]
    # the whole email INFO source contains that pattern, so dropping it is refused as well
    r = propose(t, "log-email-app-info", "drop")
    assert r["rejected_by"] == "usage"


def test_queried_data_is_never_removed_completely(clean: tuple[Session, SiftTools]) -> None:
    _, t = clean
    r = propose(t, "log-cart-app-warn", "drop")  # its pattern was queried twice
    assert r["rejected_by"] == "usage" and "2 times" in r["reason"]


def test_one_proposal_per_signal_and_a_threshold(clean: tuple[Session, SiftTools]) -> None:
    _, t = clean
    assert propose(t, "log-payment-app-debug", "drop")["created"]
    again = propose(t, "log-payment-app-debug", "drop")
    assert again["rejected_by"] == "duplicate"
    small = propose(t, "log-frontend-app-warn", "drop")  # about €12 a month
    assert small["rejected_by"] == "threshold"


def test_explanations_are_templates_with_sourced_numbers(clean: tuple[Session, SiftTools]) -> None:
    _, t = clean
    t.call("get_usage", {"signal_id": "log-payment-app-debug"})
    d = t.call("draft_rule", {"signal_id": "log-payment-app-debug", "action": "drop"})
    t.call("validate_rule", {"draft_id": d["draft_id"]})
    base = {"draft_id": d["draft_id"], "finding_class": "over_verbose_level", "confidence": "high"}
    bad = t.call("create_proposal", {**base, "explanation": ["{nope}", "b.", "c."]})
    assert bad["rejected_by"] == "text" and "nope" in bad["reason"]
    invented = t.call(
        "create_proposal", {**base, "explanation": ["It saves €9,999 a month.", "b.", "c."]}
    )
    assert invented["rejected_by"] == "text" and "9,999" in invented["reason"]
    ok = t.call("create_proposal", {**base, "explanation": EXPLAIN})
    assert ok["created"] and "€" in ok["explanation_rendered"][2]


def test_apply_never_happens_from_the_agent(clean: tuple[Session, SiftTools]) -> None:
    _, t = clean
    propose(t, "log-payment-app-debug", "drop")
    r = t.call("apply_proposal", {"proposal_id": "p-payment-app-debug-drop"})
    assert r["applied"] is False and r["status"] == "awaiting_approval"


def test_fixture_mode_has_no_synthetic_dry_runs(tmp_path: Path) -> None:
    """Without the recorder a dry run nobody recorded is an error, not a made-up answer."""
    cfg = dataclasses.replace(load_config(), db_path=tmp_path / "sift.db")
    db = make_engine(cfg.db_path)
    fixtures.seed(db, cfg, utcnow(), history=False)
    conn = fixture_connectors(cfg.fixtures_dir / "dynatrace", utcnow())
    with Session(db) as s:
        t = SiftTools(s, conn.grail, utcnow)
        d = t.call(
            "draft_rule",
            {"signal_id": "log-payment-app-debug", "action": "drop", "keep_phrase": "zzz"},
        )
        with pytest.raises(FixtureMissingError):
            t.validate_rule(d["draft_id"])


def test_rules_target_the_same_records_in_both_languages(clean: tuple[Session, SiftTools]) -> None:
    s, _ = clean
    w = engine.load_world(s, utcnow())
    d = rules.build(
        w,
        w.signals["log-cart-app-warn-timeout-calling-inventory-65c2"],
        "drop",
        "d1",
        keep_phrase="InventoryClient.reserve",
    )
    assert d.openpipeline["matcher"] == (
        'service.name == "cart" and log.source == "app" and loglevel == "WARN" and '
        'matchesPhrase(content, "Timeout calling inventory") and '
        'not matchesPhrase(content, "InventoryClient.reserve")'
    )
    assert 'not IsMatch(log.body, "(?i)InventoryClient[.]reserve")' in d.otel["code"]
    assert rules.otel_structure_problems(d.otel["code"]) == []
    no_key = rules.otel_structure_problems(d.otel["code"].replace("log_conditions", "conds"))
    assert any("needs exactly one of" in p for p in no_key)
    bad_ottl = rules.otel_structure_problems(d.otel["code"].replace("IsMatch(", "Match(("))
    assert any("unbalanced" in p for p in bad_ottl)
    assert any("unknown OTTL function Match()" in p for p in bad_ottl)
    with pytest.raises(rules.DraftError, match="Sampling"):
        rules.build(w, w.signals["log-payment-app-debug"], "sample", "d2")


def test_number_checker() -> None:
    text = (
        "payment writes 4.1 M DEBUG records a day [1], about €828 a month, "
        "35.0 % of its logs. k8s, gemini-3.7-flash, INC-2291 and the 3rd copy are "
        "not quantities; 45 is."
    )
    assert checker.numbers(text) == ["4.1", "828", "35.0", "45"]
    src = [{"records": "4.1 M", "eur": "€828", "share": 35}]
    assert checker.unbacked([text], src) == ["45"]
    assert checker.unbacked(["€1,321 and 1321.0"], [{"x": "€1,321"}]) == []


# --- acceptance: the recorded real run, replayed offline ------------------------------

EXPECTED = {  # DEMO_WORLD cases 1-6: signal -> action
    "log-payment-app-debug": "drop",
    "span-frontend-get-healthz": "drop",
    "log-cart-app-warn-timeout-calling-inventory-65c2": "drop",
    "log-inventory-app-info-heartbeat-ok-worker-8730": "logs_to_metric",
    "metric-frontend-http-server-request-duration-dim-user-id": "drop_field",
    "log-email-app-info-sent-confirmation-to-1f3c": "redact",
}
CASE_7 = (
    "log-recommendation-app-error",
    "log-recommendation-app-error-modelweightsexpiredexception-weights-older-e4d4",
)
CASE_8 = "log-checkout-app-info"


def _no_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*a: object, **k: object) -> None:
        raise AssertionError("fixture mode must not call Gemini")

    monkeypatch.setattr("server.agent.gemini.vertex_transport", refuse)
    monkeypatch.setattr("server.agent.gemini.GeminiClient.generate", refuse)


def test_recorded_analysis_replays_offline_with_cases_1_to_8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlmodel import col, select

    from server.agent import runner as ar
    from server.models import RunEvent

    _no_gemini(monkeypatch)
    cfg = dataclasses.replace(load_config(), db_path=tmp_path / "sift.db")
    rec = ar.load_recording(cfg.fixtures_dir / "agent" / "analyze.json")
    assert rec is not None and rec["state"] == "clean"
    assert rec["usage"]["served_model"] and rec["usage"]["tokens_in"] > 0
    db = make_engine(cfg.db_path)
    now = utcnow()
    fixtures.seed(db, cfg, now, history=False)
    with Session(db, expire_on_commit=False) as s:
        grail = fixture_connectors(cfg.fixtures_dir / "dynatrace", now).grail  # no recorder
        log = ar.RunLog(s, "run_acc", lambda: now)
        gen = ar.replay(s, grail, lambda: now, rec, run_id="run_acc", log=log)
        out = ar.run_replay_sync(gen, lambda ms: None)
        log.finish()

        # Same data as the recording: every tool result matches, the texts are replayed.
        assert not out.diverged and out.unbacked == []
        assert out.summary == rec["final_text"]
        w = engine.load_world(s, now)
        made = {p.signal_id: p for p in w.proposals}
        for sid, action in EXPECTED.items():
            assert sid in made and made[sid].action == action, sid
        assert made["log-cart-app-warn-timeout-calling-inventory-65c2"].reduction_ratio == (
            pytest.approx(2 / 3, abs=1e-3)
        )
        assert not {*CASE_7, CASE_8} & set(made)  # cases 7 and 8: no proposal

        events = s.exec(
            select(RunEvent).where(RunEvent.run_id == "run_acc").order_by(col(RunEvent.seq))
        ).all()
        # ...and the run log says why: guardrails, checked by code at the end of the run
        closing = [e for e in events if e.tool == "summarize_run"][-1]
        protected = {x["signal"]: x["guardrails"] for x in closing.result["result"][
            "protected_left_alone"]}  # fmt: skip
        assert protected[CASE_8] == ["Data an alert, SLO or workflow read in the last 90 days"]
        assert protected[CASE_7[0]] == ["ERROR and FATAL logs"]
        assert "ERROR and FATAL logs" in closing.label and CASE_8 not in made
        # the agent's advice for case 7 is recorded on the signal (rendered with live numbers)
        pattern = w.signals[CASE_7[1]]
        assert pattern.finding_class == "noise" and pattern.finding_note

        # No invented numbers: every agent text (messages, rendered explanations, the
        # finding) only uses numbers that appear in this run's tool results.
        results = [e.result["result"] for e in events if e.type == "tool"]
        texts = [e.label for e in events if e.type == "message"]
        for p in w.proposals:
            ctx = engine.proposal_context(w, p)
            texts += [engine.render(t, ctx) for t in p.explanation]
        texts.append(engine.render(pattern.finding_note, engine.signal_context(w, pattern)))
        assert texts and checker.unbacked(texts, results) == []


def test_the_seeded_demo_replays_the_same_run_on_its_own_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In the demo the run happens 14 minutes ago, after cases 4 and 5 were verified and
    static spans were rejected: the replay re-checks every call and skips those."""
    from sqlmodel import select

    from server.models import Run, RunEvent

    _no_gemini(monkeypatch)
    cfg = dataclasses.replace(load_config(), db_path=tmp_path / "sift.db")
    db = make_engine(cfg.db_path)
    fixtures.seed(db, cfg, utcnow())
    with Session(db) as s:
        run = s.exec(select(Run).where(Run.type == "analyze")).one()
        assert run.replay_of == "agent/analyze.json" and run.scripted
        evs = s.exec(select(RunEvent).where(RunEvent.run_id == run.id)).all()
        made = {e.result["proposal"] for e in evs if e.type == "proposal"}
        assert made == {
            "p-payment-app-debug-drop",
            "p-frontend-get-healthz-drop",
            "p-cart-app-warn-timeout-calling-inventory-65c2-drop",
            "p-email-app-info-sent-confirmation-to-1f3c-redact",
        }
        rejected = {
            e.result["args"]["draft_id"]: e.result["result"]["rejected_by"]
            for e in evs
            if e.tool == "create_proposal" and not e.result["result"].get("created")
        }
        assert sorted(rejected.values()) == ["duplicate", "duplicate", "guardrail"]
        assert run.summary.startswith("Created 4 new proposals worth €1,321 a month.")
