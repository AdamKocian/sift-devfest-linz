"""API tests on the fixture world (SIFT_MODE=fixture, temporary SQLite DB)."""

from __future__ import annotations

import json
import re
import time
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from server import engine, fixtures, services
from server.config import Config
from server.db import make_engine, utcnow
from server.main import create_app


def sse_events(text: str) -> list[tuple[str, Any]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln and not ln.startswith(":")]
        if not lines:
            continue
        event = next(ln[7:] for ln in lines if ln.startswith("event: "))
        data = json.loads(next(ln[6:] for ln in lines if ln.startswith("data: ")))
        out.append((event, data))
    return out


# Numbers that carry meaning: money, volume, record counts, percentages.
NUMBER = re.compile(r"€[\d,]+|\d[\d,.]*\s?(?:GiB|M|k|B|%)")


def unbacked_numbers(text: str, results: list[Any]) -> list[str]:
    blob = json.dumps(results, ensure_ascii=False)
    return [n for n in NUMBER.findall(text) if n not in blob]


def finish_verifications(cfg: Config, seconds: int = 60) -> list[str]:
    db = make_engine(cfg.db_path)
    with Session(db) as s:
        return services.run_due_verifications(s, utcnow() + timedelta(seconds=seconds))


# --- overview ---------------------------------------------------------------------------


def test_overview_numbers_are_computed_and_consistent(client: TestClient) -> None:
    o = client.get("/api/overview").json()
    k = o["kpis"]
    awaiting = client.get("/api/proposals", params={"status": "awaiting"}).json()["items"]
    verified = client.get("/api/proposals", params={"status": "verified"}).json()["items"]

    assert k["awaiting_count"] == len(awaiting) == 4
    assert k["verified_count"] == len(verified) == 2
    assert k["waste_eur_month"] == pytest.approx(sum(p["savings"]["eur_month"] for p in awaiting))
    assert k["saved_eur_month"] == pytest.approx(sum(p["savings"]["eur_month"] for p in verified))
    assert k["ingest_7d_gib"] == pytest.approx(sum(c["gib"] for c in o["composition"]), abs=0.05)
    assert k["waste_ratio"] == pytest.approx(k["waste_gib_week"] / k["ingest_7d_gib"], abs=1e-3)
    # The old placeholder returned these literals.
    assert k["ingest_7d_gib"] != 412 and k["cost_month_eur"] != 3940
    # Awaiting list is the top 3 by savings.
    top = sorted(awaiting, key=lambda p: -p["savings"]["eur_month"])[:3]
    assert [p["id"] for p in o["awaiting"]] == [p["id"] for p in top]
    assert o["agent"]["state"] == "idle"
    assert o["agent"]["last_run"]["status"] == "succeeded"
    assert o["failed_verifications"] == {
        "count": 1,
        "latest_proposal_id": "p-inventory-dbstatement",
    }


def test_savings_follow_the_rates_in_settings(client: TestClient) -> None:
    before = client.get("/api/proposals/p-payment-debug").json()["savings"]["eur_month"]
    r = client.put("/api/settings", json={"rates_eur_per_gib": {"logs": 1.2}})
    assert r.status_code == 200
    assert r.json()["values"]["rates_eur_per_gib"]["spans"] == 2.1  # untouched
    after = client.get("/api/proposals/p-payment-debug").json()["savings"]["eur_month"]
    assert after == pytest.approx(before / 2)
    changes = client.get("/api/activity", params={"type": "settings"}).json()["items"]
    assert changes[0]["summary"] == "Changed the log rate from €2.40 per GiB to €1.20 per GiB"


def test_settings_validation(client: TestClient) -> None:
    assert client.put("/api/settings", json={"rates_eur_per_gib": {"logs": -1}}).status_code == 422
    assert client.put("/api/settings", json={"verify_window_min": 0}).status_code == 422


# --- signals ----------------------------------------------------------------------------


def test_signal_filters(client: TestClient) -> None:
    all_ = client.get("/api/signals").json()
    assert all_["total"] == len(all_["items"]) == 36
    costs = [s["cost_month"] for s in all_["items"]]
    assert costs == sorted(costs, reverse=True)

    spans = client.get("/api/signals", params={"kind": "span"}).json()["items"]
    assert spans and all(s["kind"] == "span" for s in spans)
    payment = client.get("/api/signals", params={"service": "payment"}).json()["items"]
    assert {s["service"] for s in payment} == {"payment"}
    unused = client.get("/api/signals", params={"usage": "unused"}).json()["items"]
    assert "log-payment-app-debug" in {s["id"] for s in unused}
    found = client.get("/api/signals", params={"q": "healthz"}).json()["items"]
    assert [s["id"] for s in found] == ["span-frontend-get-healthz"]


def test_planted_cases_have_the_expected_state(client: TestClient) -> None:
    def sig(sid: str) -> dict[str, Any]:
        return client.get(f"/api/signals/{sid}").json()

    # 1-6: proposals. 7: ERROR logs are protected. 8: checkout INFO is used by an alert.
    assert sig("log-payment-app-debug")["status"] == "awaiting_approval"
    assert sig("span-frontend-get-healthz")["status"] == "awaiting_approval"
    assert sig("log-cart-app-warn-timeout-calling-inventory-65c2")["status"] == "awaiting_approval"
    assert sig("log-inventory-app-info-heartbeat-ok-worker-8730")["status"] == "verified"
    assert sig("metric-frontend-http-server-request-duration-dim-user-id")["status"] == "verified"
    assert sig("log-email-app-info-sent-confirmation-to-1f3c")["status"] == "awaiting_approval"
    rec = sig("log-recommendation-app-error")
    assert rec["status"] == "protected"
    assert "ERROR and FATAL logs" in rec["protected_by"]
    assert "model-cache bug" in rec["finding"]["note"]
    checkout = sig("log-checkout-app-info")
    assert checkout["status"] == "protected" and checkout["usage"]["klass"] == "used"


def test_signal_detail_series_reflects_applied_rules(client: TestClient) -> None:
    hb = client.get("/api/signals/log-inventory-app-info-heartbeat-ok-worker-8730").json()
    assert len(hb["series"]) == 14
    assert hb["gib_day"] == 0  # the verified logs_to_metric rule removes all of it
    assert hb["series"][0]["gib"] > 2 and hb["series"][-1]["gib"] == 0
    assert hb["markers"][0]["label"] == "Rule applied"
    parent = client.get("/api/signals/log-inventory-app-info").json()
    assert parent["gib_day"] == pytest.approx(parent["baseline_gib_day"] - 18.4 / 7, abs=1e-3)


def test_dimension_impact_separates_affected_and_unaffected(client: TestClient) -> None:
    d = client.get("/api/proposals/p-http-duration-userid").json()
    assert d["impact"] and all(r["effect"] == "not_affected" for r in d["impact"])
    assert d["blast_radius"]["dashboard"] == 0 and d["blast_radius"]["alert"] == 0
    assert any("not user.id" in r["effect_note"] for r in d["impact"])


# --- proposals --------------------------------------------------------------------------


def test_approve_verify_flow_is_audited_and_persists(cfg: Config) -> None:
    with TestClient(create_app(cfg)) as client:
        detail = client.get("/api/proposals/p-payment-debug").json()
        assert detail["blast_radius"]["dashboard"] == 0 and detail["blast_radius"]["alert"] == 0
        assert detail["dry_run"]["errors_matched"] == 0
        assert len(detail["explanation"]) == 5 and "[1]" in detail["explanation"][0]

        r = client.post("/api/proposals/p-payment-debug/approve")
        assert r.status_code == 200 and r.json()["status"] == "verifying"
        actions = [h["action"] for h in r.json()["history"]]
        assert actions == ["proposal_created", "approved", "applied", "verification_started"]
        assert r.json()["history"][1]["actor"] == "Lena Hofer"

    assert finish_verifications(cfg) == ["p-payment-debug"]

    with TestClient(create_app(cfg)) as client:  # a new process sees the same state
        d = client.get("/api/proposals/p-payment-debug").json()
        assert d["status"] == "verified"
        assert d["verify_result"]["passed"] is True
        assert [h["action"] for h in d["history"]][-1] == "verified"
        assert client.get("/api/overview").json()["kpis"]["verified_count"] == 3
        assert client.get("/api/signals/log-payment-app-debug").json()["gib_day"] == 0
        # Today's bar already shows the drop for the rest of the day.
        series = d["series"]
        assert series[-1]["gib"] < series[-2]["gib"]


def test_reject_and_revert(client: TestClient) -> None:
    r = client.post("/api/proposals/p-cart-dup-trace/reject", json={"reason": "Not now"})
    assert r.json()["status"] == "rejected" and r.json()["reject_reason"] == "Not now"
    assert client.post("/api/proposals/p-cart-dup-trace/approve").status_code == 409

    r = client.post("/api/proposals/p-inventory-heartbeat/revert")
    assert r.json()["status"] == "reverted"
    hb = client.get("/api/signals/log-inventory-app-info-heartbeat-ok-worker-8730").json()
    assert hb["gib_day"] == pytest.approx(hb["baseline_gib_day"])
    assert client.post("/api/proposals/p-inventory-heartbeat/revert").status_code == 409
    assert client.post("/api/proposals/nope/approve").status_code == 404


def test_failed_verification_reverts_automatically(client: TestClient, cfg: Config) -> None:
    client.put("/api/settings", json={"fixture_fail_next_verification": True})
    client.post("/api/proposals/p-frontend-healthz/approve")
    assert finish_verifications(cfg) == ["p-frontend-healthz"]
    d = client.get("/api/proposals/p-frontend-healthz").json()
    assert d["status"] == "reverted"
    assert d["verify_result"]["passed"] is False
    assert "reverted" in d["verify_result"]["incident"]
    assert [h["action"] for h in d["history"]][-2:] == ["verification_failed", "reverted"]
    assert client.get("/api/settings").json()["values"]["fixture_fail_next_verification"] is False
    assert client.get("/api/overview").json()["failed_verifications"]["count"] == 2


def test_guardrail_blocks_apply(client: TestClient) -> None:
    r = client.post(
        "/api/guardrails",
        json={
            "name": "payment debug",
            "reason": "Needed for PCI audit",
            "service": "payment",
            "kind": "log_source",
            "key_pattern": "app · DEBUG",
        },
    )
    assert r.status_code == 201 and [m["id"] for m in r.json()["matches"]] == [
        "log-payment-app-debug"
    ]
    r = client.post("/api/proposals/p-payment-debug/approve")
    assert r.status_code == 409 and "guardrail" in r.json()["detail"]
    assert client.delete("/api/guardrails/g-error-levels").status_code == 409
    assert client.delete(f"/api/guardrails/{'g-payment-debug'}").status_code == 204
    assert client.post("/api/proposals/p-payment-debug/approve").status_code == 200


def test_no_open_proposal_drops_protected_data(cfg: Config) -> None:
    """AGENTS.md guardrails: never propose dropping ERROR/FATAL, audit logs, or data an
    alert, SLO or workflow read in the last 90 days. Redaction is allowed."""
    db = make_engine(cfg.db_path)
    fixtures.seed(db, cfg, utcnow())
    with Session(db) as s:
        w = engine.load_world(s, utcnow())
        for p in w.proposals:
            if p.status in ("rejected", "reverted"):
                continue
            assert engine.blocking_guardrails(w, p) == [], p.id
        protected = {
            "log-recommendation-app-error",
            "log-recommendation-app-error-modelweightsexpiredexception-weights-older-e4d4",
            "log-checkout-app-info",
            "log-checkout-audit-info",
        }
        assert not any(p.signal_id in protected for p in w.proposals)


def test_every_explanation_renders(client: TestClient) -> None:
    for p in client.get("/api/proposals").json()["items"]:
        d = client.get(f"/api/proposals/{p['id']}").json()
        assert all("{" not in s for s in d["explanation"]), p["id"]
        assert len(d["evidence"]) == 4


# --- runs -------------------------------------------------------------------------------


def _wait_run(client: TestClient, rid: str) -> dict[str, Any]:
    for _ in range(100):
        run = client.get(f"/api/runs/{rid}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_scripted_analysis_run_streams_and_backs_every_number(client: TestClient) -> None:
    r = client.post("/api/runs", json={"type": "analyze"})
    assert r.status_code == 201
    run = _wait_run(client, r.json()["id"])
    assert run["status"] == "succeeded"
    assert run["summary"].startswith("No new findings")  # the seeded run created them
    assert run["served_model"] and run["tokens_in"] > 0

    events = sse_events(client.get(f"/api/runs/{run['id']}/events").text)
    kinds = [e for e, _ in events]
    assert kinds[-1] == "run" and kinds.count("run_event") == len(run["events"])
    assert run["events"][-1]["type"] == "end"
    results = [e["result"] for e in run["events"] if e["type"] == "tool"]
    assert unbacked_numbers(run["summary"], results) == []
    assert re.findall(r"\d+", run["summary"])  # it does contain numbers


def test_seeded_run_created_the_open_proposals(client: TestClient) -> None:
    runs = client.get("/api/runs").json()["items"]
    assert len(runs) == 1 and runs[0]["trigger"] == "schedule"
    run = client.get(f"/api/runs/{runs[0]['id']}").json()
    created = [e["result"]["proposal"] for e in run["events"] if e["type"] == "proposal"]
    assert sorted(created) == sorted(
        ["p-payment-debug", "p-frontend-healthz", "p-cart-dup-trace", "p-email-pii"]
    )
    results = [e["result"] for e in run["events"] if e["type"] == "tool"]
    assert unbacked_numbers(run["summary"], results) == []


def test_verify_and_discover_runs(client: TestClient) -> None:
    client.post("/api/proposals/p-email-pii/approve")
    run = _wait_run(client, client.post("/api/runs", json={"type": "verify"}).json()["id"])
    assert run["summary"] == "1 of 1 rules verified."
    assert client.get("/api/proposals/p-email-pii").json()["status"] == "verified"
    run = _wait_run(client, client.post("/api/runs", json={"type": "discover"}).json()["id"])
    results = [e["result"] for e in run["events"] if e["type"] == "tool"]
    assert unbacked_numbers(run["summary"], results) == []
    assert client.post("/api/runs", json={"type": "nope"}).status_code == 422


def test_only_one_run_at_a_time(client: TestClient, cfg: Config) -> None:
    from server import runs

    db = make_engine(cfg.db_path)
    with Session(db) as s:
        runs.new_run(s, cfg, "analyze", "user", utcnow())
    assert client.post("/api/runs", json={"type": "analyze"}).status_code == 409
    assert client.get("/api/overview").json()["agent"]["state"] == "running"


# --- chat -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Why is dropping payment DEBUG logs safe?",
        "What do checkout logs cost?",
        "Drop debug logs from payment but keep errors",
        "Can we drop the recommendation errors?",
        "Where is the biggest saving?",
        "Tell me about this",
    ],
)
def test_chat_answers_from_tool_results(client: TestClient, message: str) -> None:
    body: dict[str, Any] = {"message": message}
    if message == "Tell me about this":
        body["signal_id"] = "log-frontend-nginx-access-info"
    r = client.post("/api/chat", json=body)
    assert r.headers["content-type"].startswith("text/event-stream")
    events = sse_events(r.text)
    assert events[-1][0] == "done"
    text = "".join(d["text"] for e, d in events if e == "token")
    tools = [d["result"] for e, d in events if e == "tool"]
    assert tools and len(text) > 40
    assert unbacked_numbers(text, tools) == []
    if "payment" in message:
        assert any(e == "proposal" and d["proposal"]["id"] == "p-payment-debug" for e, d in events)
    if "recommendation" in message:
        assert "won't propose" in text and "ERROR and FATAL logs" in text


def test_chat_fallback_offers_suggestions(client: TestClient) -> None:
    events = sse_events(client.post("/api/chat", json={"message": "hello"}).text)
    assert any(e == "suggestions" for e, _ in events)


# --- demo -------------------------------------------------------------------------------


def test_demo_reset_restores_the_world(client: TestClient) -> None:
    client.post("/api/proposals/p-payment-debug/approve")
    client.put("/api/settings", json={"agent_paused": True})
    assert client.post("/api/demo/reset").json() == {"status": "ok"}
    assert client.get("/api/proposals/p-payment-debug").json()["status"] == "awaiting_approval"
    assert client.get("/api/settings").json()["values"]["agent_paused"] is False


def test_meta_and_connections(client: TestClient) -> None:
    m = client.get("/api/meta").json()
    assert m["mode"] == "fixture" and m["environment"]["id"] == "abc12345"
    assert m["awaiting_count"] == 4 and m["user"]["name"] == "Lena Hofer"
    conns = client.get("/api/connections").json()["items"]
    assert {c["id"] for c in conns} == {"dynatrace", "dynatrace-mcp", "vertex", "apply"}
    assert all(
        "token" not in json.dumps(c["fields"]).lower() or "set" in json.dumps(c["fields"])
        for c in conns
    )
    assert client.get("/health").json()["status"] == "ok"
