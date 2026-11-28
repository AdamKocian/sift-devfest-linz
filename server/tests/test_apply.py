"""Phase 04: apply targets, verification and automatic revert (offline acceptance).

- dry-run never writes; otelcol writes a Sift-managed Collector config (tmp files here);
  openpipeline writes Settings 2.0 pipeline objects (fixture settings client, and the live
  client against a mocked HTTP API). The live OpenPipeline write is untested: no tenant.
- `otelcol validate` runs only when SIFT_OTELCOL_BIN points at a collector binary."""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlmodel import Session, col, select

from server import apply, engine, fixtures, services, verify
from server.apply.openpipeline import (
    FixtureSettingsClient,
    LiveSettingsClient,
    OpenPipelineApplier,
)
from server.apply.otelcol import OtelcolApplier, validate_text
from server.config import REPO_ROOT, Config
from server.connectors.base import ConnectorError
from server.connectors.grail import Query, QueryResult
from server.connectors.mcp_client import McpTool
from server.connectors.store import FixtureStore
from server.db import make_engine, utcnow
from server.main import create_app
from server.models import AuditEvent, Proposal

BASE = REPO_ROOT / "infra" / "otel-collector" / "dynatrace.yaml"
OTELCOL_BIN = os.environ.get("SIFT_OTELCOL_BIN") or None


def target(cfg: Config, name: str, tmp_path: Path, **kw: Any) -> Config:
    return dataclasses.replace(
        cfg, apply_target=name, otelcol_config=tmp_path / "otelcol" / "config.yaml", **kw
    )


def world_and_session(cfg: Config) -> tuple[Session, engine.World]:
    db = make_engine(cfg.db_path)
    fixtures.seed(db, cfg, utcnow())
    s = Session(db, expire_on_commit=False)
    return s, engine.load_world(s, utcnow())


def approved(p: Proposal) -> Proposal:
    """A detached copy in the state services.approve leaves it in before writing."""
    q = Proposal.model_validate(p.model_dump())
    q.status, q.decided_by, q.rule_version = "approved", "Lena Hofer", apply.rule_version(p.rule)
    return q


def finish(cfg: Config, seconds: int = 60) -> list[str]:
    with Session(make_engine(cfg.db_path)) as s:
        return services.run_due_verifications(s, cfg, utcnow() + timedelta(seconds=seconds))


def history(cfg: Config, pid: str) -> list[AuditEvent]:
    with Session(make_engine(cfg.db_path)) as s:
        stmt = (
            select(AuditEvent)
            .where(AuditEvent.subject_id == pid)
            .order_by(col(AuditEvent.at), col(AuditEvent.id))
        )
        return list(s.exec(stmt).all())


# --- approval is the only way in ----------------------------------------------------------


def test_write_refuses_unapproved_or_changed_rules(cfg: Config, tmp_path: Path) -> None:
    s, w = world_and_session(cfg)
    p = w.proposal("p-payment-app-debug-drop")
    assert p is not None and p.status == "awaiting_approval"
    for t in ("dry-run", "otelcol", "openpipeline"):
        applier = apply.applier_for(target(cfg, t, tmp_path), t)
        with pytest.raises(apply.ApplyError, match="only an approved proposal"):
            applier.write(p, applier.snapshot(p))
    q = approved(p)
    q.rule = {**q.rule, "otel": {**q.rule["otel"], "code": q.rule["otel"]["code"] + "\n"}}
    with pytest.raises(apply.ApplyError, match="changed after it was approved"):
        apply.applier_for(cfg, "dry-run").write(q, {})
    assert not (tmp_path / "otelcol").exists()


def test_approval_records_user_and_rule_version(client: TestClient) -> None:
    pid = "p-payment-app-debug-drop"
    version = client.get(f"/api/proposals/{pid}").json()["rule_version"]
    r = client.post(f"/api/proposals/{pid}/approve", json={"rule_version": "r-00000000"})
    assert r.status_code == 409 and "changed since you opened it" in r.json()["detail"]
    r = client.post(f"/api/proposals/{pid}/approve", json={"rule_version": version})
    assert r.status_code == 200
    approved_ev = next(h for h in r.json()["history"] if h["action"] == "approved")
    assert approved_ev["actor"] == "Lena Hofer"
    assert approved_ev["after"] == {
        "status": "approved",
        "rule_version": version,
        "target": "dry-run",
    }
    applied_ev = next(h for h in r.json()["history"] if h["action"] == "applied")
    assert applied_ev["summary"].startswith("Applied the rule: Dry run: nothing was written")
    assert applied_ev["after"]["ref"]["dry_run"] is True


# --- otelcol --------------------------------------------------------------------------------


def otel_applier(tmp_path: Path, **kw: Any) -> OtelcolApplier:
    return OtelcolApplier(tmp_path / "otelcol" / "config.yaml", BASE, **kw)


def test_otelcol_write_is_idempotent_and_revert_restores_the_snapshot(
    cfg: Config, tmp_path: Path
) -> None:
    s, w = world_and_session(cfg)
    p = approved(w.proposal("p-payment-app-debug-drop"))  # type: ignore[arg-type]
    kills: list[tuple[int, int]] = []
    a = otel_applier(tmp_path, reload="pid:4242", kill=lambda pid, sig: kills.append((pid, sig)))
    snap = a.snapshot(p)
    assert snap["content"] == BASE.read_text() and snap["existed"] is False
    p.previous_state = snap
    res = a.write(p, snap)
    p.applied_ref = res.ref
    doc = yaml.safe_load(a.path.read_text())
    cid = "filter/sift-p-payment-app-debug-drop"
    assert cid in doc["processors"] and res.ref["components"] == [cid]
    assert doc["service"]["pipelines"]["logs"]["processors"] == ["memory_limiter", cid, "batch"]
    assert cid not in doc["service"]["pipelines"]["traces"]["processors"]
    assert kills == [(4242, signal.SIGHUP)]
    assert a.path.read_text().startswith("# Managed by Sift")

    again = a.write(p, snap)  # idempotent: same rule, nothing changes, no second reload
    assert again.changed is False and len(kills) == 1

    r = a.revert(p)
    assert r.restored == "snapshot" and a.path.read_text() == snap["content"]
    assert len(kills) == 2


def test_otelcol_revert_after_someone_else_edited_removes_only_this_rule(
    cfg: Config, tmp_path: Path
) -> None:
    s, w = world_and_session(cfg)
    p = approved(w.proposal("p-frontend-get-healthz-drop"))  # type: ignore[arg-type]
    a = otel_applier(tmp_path)
    p.previous_state = a.snapshot(p)
    p.applied_ref = a.write(p, p.previous_state).ref
    doc = yaml.safe_load(a.path.read_text())
    doc["processors"]["attributes/team"] = {
        "actions": [{"key": "team", "value": "web", "action": "insert"}]
    }
    doc["service"]["pipelines"]["traces"]["processors"].insert(0, "attributes/team")
    a.path.write_text(yaml.safe_dump(doc, sort_keys=False))
    r = a.revert(p)
    assert r.restored == "removed_own"
    after = yaml.safe_load(a.path.read_text())
    assert "attributes/team" in after["processors"]
    assert "filter/sift-p-frontend-get-healthz-drop" not in after["processors"]
    assert "filter/sift-p-frontend-get-healthz-drop" not in json.dumps(after["service"])


def test_otelcol_logs_to_metric_counts_before_the_filter(cfg: Config, tmp_path: Path) -> None:
    s, w = world_and_session(cfg)
    p = approved(w.proposal("p-inventory-heartbeat"))  # type: ignore[arg-type]
    a = otel_applier(tmp_path)
    a.write(p, a.snapshot(p))
    doc = yaml.safe_load(a.path.read_text())
    name = "sift-p-inventory-heartbeat"
    pl = doc["service"]["pipelines"]
    assert f"count/{name}" in doc["connectors"]
    assert pl[f"logs/{name}"] == {
        "receivers": ["otlp"],
        "processors": ["memory_limiter", "batch"],
        "exporters": [f"count/{name}"],
    }
    assert pl[f"metrics/{name}"]["receivers"] == [f"count/{name}"]
    assert pl[f"metrics/{name}"]["exporters"] == ["otlp_http"]
    assert "cumulative_to_delta" in pl[f"metrics/{name}"]["processors"]
    assert f"filter/{name}" in pl["logs"]["processors"]


def test_otelcol_failed_reload_or_validation_leaves_the_old_file(
    cfg: Config, tmp_path: Path
) -> None:
    s, w = world_and_session(cfg)
    p = approved(w.proposal("p-payment-app-debug-drop"))  # type: ignore[arg-type]

    def docker_fails(*a: Any, **k: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(a[0], 1, "", "No such container: collector")

    a = otel_applier(tmp_path, reload="docker:collector", run=docker_fails)
    a.path.parent.mkdir(parents=True)
    a.path.write_text(BASE.read_text())
    with pytest.raises(apply.ApplyError, match="previous config restored"):
        a.write(p, a.snapshot(p))
    assert a.path.read_text() == BASE.read_text()

    fake = tmp_path / "otelcol-fails"
    fake.write_text(
        "#!/bin/sh\necho 'Error: invalid configuration: processors::filter' >&2\nexit 1\n"
    )
    fake.chmod(0o755)
    b = otel_applier(tmp_path, otelcol_bin=str(fake))
    with pytest.raises(apply.ApplyError, match="otelcol validate failed"):
        b.write(p, b.snapshot(p))
    assert b.path.read_text() == BASE.read_text()


@pytest.mark.skipif(not OTELCOL_BIN, reason="otelcol validate: not run (set SIFT_OTELCOL_BIN)")
def test_managed_configs_pass_otelcol_validate(cfg: Config, tmp_path: Path) -> None:
    s, w = world_and_session(cfg)
    a = otel_applier(tmp_path, otelcol_bin=OTELCOL_BIN)
    for p in w.proposals:
        if (p.rule.get("otel") or {}).get("code"):
            a.write(approved(p), {})  # raises when validate fails
    assert validate_text(a.path.read_text(), OTELCOL_BIN) == "passed"


# --- openpipeline -------------------------------------------------------------------------


def fixture_op(cfg: Config, tmp_path: Path, pipeline: str = "noisy-shop") -> OpenPipelineApplier:
    client = FixtureSettingsClient(FixtureStore(cfg.fixtures_dir / "dynatrace"), tmp_path)
    return OpenPipelineApplier(client, pipeline)


def test_openpipeline_snapshot_write_revert(cfg: Config, tmp_path: Path) -> None:
    s, w = world_and_session(cfg)
    a = fixture_op(cfg, tmp_path)
    p = approved(w.proposal("p-inventory-heartbeat"))  # type: ignore[arg-type]
    snap = a.snapshot(p)
    assert snap["schemaId"] == "builtin:openpipeline.logs.pipelines"
    before = snap["value"]
    p.previous_state = snap
    res = a.write(p, snap)
    p.applied_ref = res.ref
    value = a.client.get_object(snap["objectId"])["value"]
    storage = [x["id"] for x in value["storage"]["processors"]]
    metric = [x["id"] for x in value["metricExtraction"]["processors"]]
    assert storage == ["sift-p-inventory-heartbeat-storage", "default-bucket"]  # first match
    assert metric == ["sift-p-inventory-heartbeat-metric"]
    proc = value["metricExtraction"]["processors"][0]
    assert proc["type"] == "counterMetric" and proc["counterMetric"]["metricKey"].startswith("log.")
    assert "p-inventory-heartbeat" in proc["description"]
    assert a.write(p, snap).changed is False  # idempotent
    r = a.revert(p)
    assert r.restored == "snapshot"
    assert a.client.get_object(snap["objectId"])["value"] == before


def test_openpipeline_unknown_pipeline_fails_cleanly(cfg: Config) -> None:
    world_and_session(cfg)  # seed the history with dry-run first
    cfg2 = dataclasses.replace(cfg, apply_target="openpipeline", openpipeline_pipeline="nope")
    with TestClient(create_app(cfg2)) as c:
        r = c.post("/api/proposals/p-payment-app-debug-drop/approve")
        assert r.status_code == 409 and "no pipeline with customId 'nope'" in r.json()["detail"]
        d = c.get("/api/proposals/p-payment-app-debug-drop").json()
        assert d["status"] == "awaiting_approval"
        assert [h["action"] for h in d["history"]][-2:] == ["approved", "apply_failed"]


def test_openpipeline_live_client_against_mocked_settings_api(cfg: Config) -> None:
    s, w = world_and_session(cfg)
    p = approved(w.proposal("p-payment-app-debug-drop"))  # type: ignore[arg-type]
    obj = json.loads(
        (cfg.fixtures_dir / "dynatrace" / "settings" / "openpipeline_pipelines.json").read_text()
    )["response"]["items"][0]
    state = {"value": obj["value"], "token": "tok-1"}
    calls: list[tuple[str, str, dict[str, Any], Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, dict(req.url.params), body))
        assert req.headers["Authorization"] == "Bearer platform-token"
        if req.method == "GET" and req.url.path == "/api/v2/settings/objects":
            if "nextPageKey" not in req.url.params:
                return httpx.Response(200, json={"items": [], "nextPageKey": "page2"})
            assert dict(req.url.params) == {"nextPageKey": "page2"}
            item = {**obj, "value": state["value"], "updateToken": state["token"]}
            return httpx.Response(200, json={"items": [item], "nextPageKey": None})
        if req.method == "GET":
            return httpx.Response(
                200, json={**obj, "value": state["value"], "updateToken": state["token"]}
            )
        assert req.method == "PUT" and req.url.path == f"/api/v2/settings/objects/{obj['objectId']}"
        if body.get("updateToken") != state["token"]:
            return httpx.Response(409, json={"error": {"code": 409, "message": "changed"}})
        if req.url.params["validateOnly"] == "false":
            state["value"], state["token"] = body["value"], "tok-2"
        return httpx.Response(200, json={"objectId": obj["objectId"]})

    http = httpx.Client(
        base_url="https://abc12345.live.dynatrace.com",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer platform-token"},
    )
    a = OpenPipelineApplier(LiveSettingsClient("https://x", "platform-token", http), "noisy-shop")
    p.previous_state = a.snapshot(p)
    p.applied_ref = a.write(p, p.previous_state).ref
    first = calls[0]
    assert first[2]["schemaIds"] == "builtin:openpipeline.logs.pipelines"
    assert first[2]["fields"] == "objectId,value,updateToken,schemaVersion,schemaId"
    puts = [c for c in calls if c[0] == "PUT"]
    assert [c[2]["validateOnly"] for c in puts] == ["true", "false"]
    assert all(c[3]["updateToken"] == "tok-1" and c[3]["schemaVersion"] == "1.0.0" for c in puts)
    procs = state["value"]["processing"]["processors"]
    assert procs[-1]["id"] == "sift-p-payment-app-debug-drop" and procs[-1]["type"] == "drop"
    assert set(procs[-1]) == {"id", "type", "matcher", "description", "enabled"}
    a.revert(p)
    assert state["value"] == obj["value"]


# --- verification: recorded, derived, live -------------------------------------------------


def test_recorded_failure_auto_reverts_and_restores_the_snapshot(
    cfg: Config, tmp_path: Path
) -> None:
    """The scenario applies p-inventory-dbstatement 6 days ago; its recording says the
    "Inventory DB latency" dashboard went empty, so seeding reverts it automatically."""
    for t in ("otelcol", "openpipeline"):
        c = target(cfg, t, tmp_path / t, db_path=tmp_path / t / "sift.db")
        s, w = world_and_session(c)
        p = w.proposal("p-inventory-dbstatement")
        assert p is not None and p.status == "reverted"
        assert p.verify_result["source"] == "recorded"
        assert p.verify_result["incident_by"] == "agent (recorded)"
        refs = p.verify_result["checks"][1]
        assert refs["passed"] is False and "Inventory DB latency" in refs["detail"]
        ev = history(c, p.id)
        assert [e.action for e in ev][-2:] == ["verification_failed", "reverted"]
        assert ev[-1].after["restored"] == "snapshot"
        verified = w.proposal("p-http-duration-userid")
        assert verified is not None and verified.status == "verified"
        if t == "otelcol":
            text = c.otelcol_config.read_text()
            assert "sift-p-inventory-dbstatement" not in text
            assert "transform/sift-p-http-duration-userid" in text  # still applied
        else:
            spans = FixtureSettingsClient(
                FixtureStore(c.fixtures_dir / "dynatrace"), apply.fixture_state_dir(c)
            )
            value = spans.get_object("fixture-openpipeline-spans-noisy-shop")["value"]
            assert all("dbstatement" not in x["id"] for x in value["processing"]["processors"])
            metrics = spans.get_object("fixture-openpipeline-metrics-noisy-shop")["value"]
            assert metrics["processing"]["processors"][-1]["id"] == "sift-p-http-duration-userid"


def test_approve_apply_otelcol_then_verify_pass(cfg: Config, tmp_path: Path) -> None:
    c = target(cfg, "otelcol", tmp_path)
    with TestClient(create_app(c)) as client:
        r = client.post("/api/proposals/p-payment-app-debug-drop/approve")
        assert r.status_code == 200 and r.json()["status"] == "verifying"
        assert "filter/sift-p-payment-app-debug-drop" in c.otelcol_config.read_text()
    assert finish(c) == ["p-payment-app-debug-drop"]
    with TestClient(create_app(c)) as client:
        d = client.get("/api/proposals/p-payment-app-debug-drop").json()
        assert d["status"] == "verified" and d["verify_result"]["source"] == "recorded"
        assert d["applied_ref"]["components"] == ["filter/sift-p-payment-app-debug-drop"]
        vol = d["verify_result"]["checks"][0]
        assert vol["passed"] and "of the predicted drop" in vol["detail"]


def test_fail_next_verification_reverts_otelcol_rule(cfg: Config, tmp_path: Path) -> None:
    c = target(cfg, "otelcol", tmp_path)
    with TestClient(create_app(c)) as client:
        before = c.otelcol_config.read_text() if c.otelcol_config.exists() else BASE.read_text()
        client.put("/api/settings", json={"fixture_fail_next_verification": True})
        client.post("/api/proposals/p-frontend-get-healthz-drop/approve")
    assert finish(c) == ["p-frontend-get-healthz-drop"]
    with TestClient(create_app(c)) as client:
        d = client.get("/api/proposals/p-frontend-get-healthz-drop").json()
        assert d["status"] == "reverted" and d["verify_result"]["incident_by"] == "sift"
        assert "Next step:" in d["verify_result"]["incident"]
    assert c.otelcol_config.read_text() == before


def test_volume_below_70_percent_of_prediction_fails(cfg: Config) -> None:
    s, w = world_and_session(cfg)
    p = w.proposal("p-payment-app-debug-drop")
    assert p is not None
    pl = verify.plan(w, p, 30)

    class Ev(verify.DerivedEvidence):
        def volume(self, q: Query) -> dict[str, Any]:
            return {"before": 1000, "after": 1000 - round(1000 * p.reduction_ratio * 0.6)}

    checks = verify.run_checks(w, p, pl, Ev(w, p, pl))
    assert (
        checks[0]["passed"] is False and "60.0\u00a0% of the predicted drop" in checks[0]["detail"]
    )


def test_recording_must_match_the_query_code(cfg: Config) -> None:
    s, w = world_and_session(cfg)
    p = w.proposal("p-payment-app-debug-drop")
    assert p is not None
    rec = verify.recording(cfg.fixtures_dir, p.id)
    assert rec is not None
    pl = verify.plan(w, p, 45)  # another window -> other query text
    with pytest.raises(ConnectorError, match="differs from the current one"):
        verify.run_checks(w, p, pl, rec)


class FakeGrail:
    def __init__(self, records: dict[str, list[dict[str, Any]]]) -> None:
        self.records, self.queries = records, []

    def query(self, q: Query) -> QueryResult:
        self.queries.append(q)
        key = q.name.split(":")[0]
        return QueryResult(self.records.get(key, [{}]), [], 0, 0, 1, None, False, [])


class FakeMcp:
    def __init__(self, result: dict[str, Any] | None) -> None:
        self.result, self.calls = result, []

    def list_tools(self) -> list[McpTool]:
        schema = {"properties": {"dqlStatement": {"type": "string"}}, "required": ["dqlStatement"]}
        return [McpTool("execute_dql", "Run DQL", schema)]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if self.result is None:
            raise ConnectorError("MCP gateway returned 503")
        return self.result


def test_live_evidence_asks_davis_problems_through_mcp(cfg: Config) -> None:
    s, w = world_and_session(cfg)
    p = w.proposal("p-frontend-get-healthz-drop")
    assert p is not None
    pl = verify.plan(w, p, 2)
    assert "from: -4m" in pl.volume.dql and "now() - 2m" in pl.volume.dql
    grail = FakeGrail({"verify_volume": [{"before": 500, "after": 10}], "verify_ref": [{"n": 1}]})
    problem = {"display_id": "P-2611001", "event.name": "Failure rate increase on frontend"}
    mcp = FakeMcp({"content": [{"type": "text", "text": json.dumps({"records": [problem]})}]})
    checks = verify.run_checks(w, p, pl, verify.LiveEvidence(grail, mcp))  # type: ignore[arg-type]
    assert mcp.calls == [("execute_dql", {"dqlStatement": pl.problems.dql})]
    assert checks[0]["passed"] and checks[1]["passed"]
    assert checks[2]["passed"] is False and "P-2611001" in checks[2]["detail"]
    ref_q = next(q for q in grail.queries if q.name.startswith("verify_ref"))
    assert ref_q.timeframe_min == 2  # dashboard tiles have no from: of their own

    down = verify.LiveEvidence(FakeGrail({"verify_problems": []}), FakeMcp(None))  # type: ignore[arg-type]
    checks = verify.run_checks(w, p, pl, down)
    assert checks[2]["passed"] and checks[2]["via"].startswith("Grail (MCP unavailable")


def test_incident_note_numbers_must_come_from_the_checks(cfg: Config) -> None:
    s, w = world_and_session(cfg)
    p = w.proposal("p-frontend-get-healthz-drop")
    assert p is not None
    checks = [
        {"name": "volume", "label": "Volume changed as predicted", "passed": True, "detail": "ok"},
        {
            "name": "references",
            "label": "Refs",
            "passed": False,
            "detail": "Dashboard “Frontend latency” returned no data after the rule was applied.",
        },
        {
            "name": "problems",
            "label": "No new Davis problems",
            "passed": True,
            "detail": "0 new problems",
        },
    ]
    good = verify.incident_note(
        p, checks, 3, writer=lambda f: "Reverted after 3 min. Dashboard empty."
    )
    assert good.by == "agent"
    bad = verify.incident_note(p, checks, 3, writer=lambda f: "It would have saved €999.")
    assert bad.by == "sift" and "Frontend latency" in bad.text
    rec = verify.incident_note(p, checks, 3, recorded="Recorded note.", writer=lambda f: "x")
    assert rec.by == "agent (recorded)"


# --- the API behind the Sift MCP server --------------------------------------------------


def test_waste_report_lists_the_planted_cases(client: TestClient) -> None:
    r = client.get("/api/report/waste", params={"window_days": 30}).json()
    by_group = {g: {x["proposal_id"] for x in items} for g, items in r["proposals"].items()}
    assert by_group["awaiting"] == {
        "p-payment-app-debug-drop",  # case 1
        "p-frontend-get-healthz-drop",  # case 2
        "p-cart-app-warn-timeout-calling-inventory-65c2-drop",  # case 3
        "p-email-app-info-sent-confirmation-to-1f3c-redact",  # case 6
    }
    assert by_group["verified"] == {"p-inventory-heartbeat", "p-http-duration-userid"}  # 4, 5
    protected = {x["signal_id"]: x["guardrails"] for x in r["protected"]}
    assert "ERROR and FATAL logs" in protected["log-recommendation-app-error"]  # case 7
    assert "log-checkout-app-info" in protected  # case 8: read by an alert
    assert r["totals"]["awaiting_savings_eur_month"] == pytest.approx(1321.02, abs=0.01)
    assert client.get("/api/report/waste", params={"window_days": 0}).status_code == 422


def test_propose_creates_a_draft_and_never_applies(cfg: Config, tmp_path: Path) -> None:
    db = make_engine(cfg.db_path)
    fixtures.seed(db, cfg, utcnow(), history=False)  # the clean world: nothing proposed yet
    with TestClient(create_app(cfg)) as client:
        r = client.post(
            "/api/proposals/propose",
            json={"instruction": "Drop debug logs from payment but keep errors"},
        ).json()
        assert r["created"] is True and r["applied"] is False and r["mode"] == "deterministic"
        assert r["proposal"]["id"] == "p-payment-app-debug-drop"
        assert r["proposal"]["status"] == "awaiting_approval"
        d = client.get("/api/proposals/p-payment-app-debug-drop").json()
        assert [h["action"] for h in d["history"]] == ["proposal_created"]
        again = client.post(
            "/api/proposals/propose", json={"instruction": "drop DEBUG logs from payment"}
        ).json()
        assert again["created"] is False and "already exists" in again["message"]
        guarded = client.post(
            "/api/proposals/propose", json={"instruction": "drop error logs from recommendation"}
        ).json()
        assert guarded["created"] is False and "ERROR and FATAL" in guarded["message"]
