"""The live agent loop (ADK Runner + SiftGemini + Sift's tools) offline: a fake transport
stands in for Vertex AI. The main model always answers 429; the fast model plays a short
scripted run. Checks the wiring, the fallback inside the agent, guardrails and the number
checker on the final text."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any

from google.genai import errors, types
from sqlmodel import Session

from server import engine, fixtures
from server.agent import prompts
from server.agent import runner as ar
from server.agent.gemini import GeminiClient
from server.agent.tools import SiftTools
from server.config import load_config
from server.connectors import fixture_connectors
from server.db import make_engine, utcnow
from server.synth import dry_run_recorder

MAIN, FAST = "gemini-4-hydrogen", "gemini-3.7-flash"
EXPLAIN = [
    "payment writes {records_day} DEBUG records a day, {share} of its logs [1].",
    "Nothing read them in the last 30 days [2].",
    "Dropping them saves about {save_eur_month} a month [3].",
]


def _call(name: str, args: dict[str, Any]) -> types.Part:
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


SCRIPT: list[types.Part] = [
    _call("get_signals", {}),
    _call("get_usage", {"signal_id": "log-payment-app-debug"}),
    _call(
        "draft_rule",
        {"signal_id": "log-payment-app-debug", "action": "drop", "title": "Drop DEBUG logs"},
    ),
    _call("validate_rule", {"draft_id": "d1"}),
    _call("estimate_savings", {"draft_id": "d1"}),
    _call(
        "create_proposal",
        {
            "draft_id": "d1",
            "finding_class": "over_verbose_level",
            "confidence": "high",
            "explanation": EXPLAIN,
        },
    ),
    _call("draft_rule", {"signal_id": "log-checkout-app-info", "action": "drop"}),
    _call(
        "create_proposal",
        {"draft_id": "d2", "finding_class": "unused", "confidence": "low", "explanation": EXPLAIN},
    ),
    types.Part(
        text="Proposed dropping payment DEBUG logs (€759 a month). Checkout INFO is protected."
    ),
]


class FakeVertex:
    def __init__(self) -> None:
        self.turn = 0
        self.models: list[str] = []

    async def __call__(self, model: str, contents: Any, config: Any) -> Any:
        self.models.append(model)
        if model == MAIN:
            raise errors.ClientError(
                429, {"error": {"code": 429, "message": "busy", "status": "RESOURCE_EXHAUSTED"}}
            )
        part = SCRIPT[self.turn]
        self.turn += 1
        return types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(role="model", parts=[part]),
                    finish_reason=types.FinishReason.STOP,
                )
            ],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=2000, candidates_token_count=40, cached_content_token_count=1500
            ),
            model_version=model,
        )


def test_adk_loop_calls_tools_falls_back_and_respects_guardrails(tmp_path: Path) -> None:
    cfg = dataclasses.replace(load_config(), db_path=tmp_path / "sift.db")
    db = make_engine(cfg.db_path)
    now = utcnow()
    fixtures.seed(db, cfg, now, history=False)
    fake = FakeVertex()

    async def no_sleep(_: float) -> None:
        return None

    GeminiClient._cooldown.clear()
    gemini = GeminiClient(MAIN, FAST, fake, sleep=no_sleep)
    with Session(db, expire_on_commit=False) as s:
        conn = fixture_connectors(cfg.fixtures_dir / "dynatrace", now)
        conn.grail.on_missing = dry_run_recorder(tmp_path / "dyn").query  # type: ignore[attr-defined]
        log = ar.RunLog(s, "run_loop", lambda: now)
        tools = SiftTools(
            s, conn.grail, utcnow, run_id="run_loop", on_tool=log.tool,
            on_proposal=log.proposal, world_time=now,
        )  # fmt: skip
        out = asyncio.run(
            ar.run_live(
                tools,
                gemini,
                model=MAIN,
                instruction=prompts.ANALYZE,
                message=prompts.ANALYZE_TASK,
                max_tokens=100_000,
                max_llm_calls=20,
                on_llm=log.llm,
            )
        )
        log.finish()

        # fallback: three 429s on the main model, then the fast model for the whole run
        assert fake.models[:4] == [MAIN, MAIN, MAIN, FAST]
        assert set(fake.models[4:]) == {FAST}
        lg = out.ledger
        assert lg.requested_model == MAIN and lg.served_model == FAST
        assert lg.fallback_reason.startswith("429 RESOURCE_EXHAUSTED on gemini-4-hydrogen")
        assert len(lg.calls) == len(SCRIPT) and lg.cost_usd and lg.cost_usd > 0
        # the tools ran through ADK, the guardrail stopped the checkout draft
        assert [c["name"] for c in out.calls][:3] == ["get_signals", "get_usage", "draft_rule"]
        assert tools.created == ["p-payment-app-debug-drop"]
        assert tools.rejections[0]["kind"] == "guardrail"
        w = engine.load_world(s, now)
        assert w.proposal("p-payment-app-debug-drop").status == "awaiting_approval"
        # the closing text quotes a number from a tool result, so it is the summary
        assert out.unbacked == [] and out.summary == out.final_text
