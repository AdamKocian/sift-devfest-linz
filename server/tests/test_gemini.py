"""The one Gemini client: backoff, fallback to the fast model, usage and cost records.
No network: the transport is a fake that raises the errors google-genai raises."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import pytest
from google.genai import errors, types

from server.agent import pricing
from server.agent.gemini import GeminiClient, GeminiError, Ledger, TokenBudgetExceeded

MAIN, FAST = "gemini-4-hydrogen", "gemini-3.7-flash"


def _429() -> errors.ClientError:
    return errors.ClientError(
        429,
        {
            "error": {
                "code": 429,
                "message": "Resource exhausted, please try again later.",
                "status": "RESOURCE_EXHAUSTED",
            }
        },
    )


def _ok(model: str, tokens_in: int = 1000, out: int = 200, thoughts: int = 50) -> Any:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part(text="ok")]),
                finish_reason=types.FinishReason.STOP,
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=tokens_in, candidates_token_count=out, thoughts_token_count=thoughts
        ),
        model_version=model,
    )


class Fake:
    """Per model: a list of outcomes (an exception to raise or None for success)."""

    def __init__(self, plan: dict[str, list[BaseException | None]]) -> None:
        self.plan = {k: list(v) for k, v in plan.items()}
        self.calls: list[str] = []

    async def __call__(self, model: str, contents: Any, config: Any) -> Any:
        self.calls.append(model)
        outcome = self.plan[model].pop(0) if self.plan[model] else None
        if outcome is not None:
            raise outcome
        return _ok(model)


def _client(fake: Fake, slept: list[float]) -> GeminiClient:
    async def sleep(s: float) -> None:
        slept.append(s)

    t = [0.0]

    def clock() -> float:
        return t[0] + sum(slept)

    GeminiClient._cooldown.clear()
    return GeminiClient(
        MAIN,
        FAST,
        fake,
        sleep=sleep,
        jitter=lambda: 0.5,  # no jitter: 3 s, then 6 s
        clock=clock,
        today=lambda: date(2026, 11, 26),
    )


def test_429_backs_off_then_falls_back_to_the_fast_model() -> None:
    fake = Fake({MAIN: [_429(), _429(), _429()], FAST: []})
    slept: list[float] = []
    c = _client(fake, slept)
    ledger = Ledger()

    resp, rec = asyncio.run(c.generate("hi", ledger=ledger, purpose="agent"))

    assert resp.text == "ok"
    assert fake.calls == [MAIN, MAIN, MAIN, FAST]
    assert slept == [3.0, 6.0]  # truncated exponential backoff, ~20 s budget
    assert rec.requested_model == MAIN and rec.served_model == FAST
    assert rec.fallback_reason == "429 RESOURCE_EXHAUSTED on gemini-4-hydrogen after 3 attempts"
    assert [a.code for a in rec.attempts] == [429, 429, 429, None]
    # recorded in the run: serving model, tokens (thinking counts as output), cost
    assert ledger.served_model == FAST and ledger.requested_model == MAIN
    assert (rec.tokens_in, rec.tokens_out, rec.thoughts) == (1000, 250, 50)
    assert rec.cost_usd == pytest.approx(1000 / 1e6 * 0.75 + 250 / 1e6 * 3.75)


def test_after_a_fallback_the_rest_of_the_run_uses_the_fast_model() -> None:
    fake = Fake({MAIN: [_429(), _429(), _429()], FAST: []})
    slept: list[float] = []
    c = _client(fake, slept)
    ledger = Ledger()
    asyncio.run(c.generate("one", ledger=ledger, purpose="agent"))
    fake.calls.clear()

    _, rec = asyncio.run(c.generate("two", ledger=ledger, purpose="agent"))

    assert fake.calls == [FAST]  # no 20 s of backoff on every later call
    assert rec.fallback_reason == "skipped: gemini-4-hydrogen failed earlier in this run"
    # another run in the next minutes skips the main model too (cool-down)
    _, rec2 = asyncio.run(c.generate("three", ledger=Ledger(), purpose="chat"))
    assert rec2.served_model == FAST and rec2.fallback_reason.startswith("skipped:")


def test_a_transient_429_recovers_on_the_main_model() -> None:
    fake = Fake({MAIN: [_429()], FAST: []})
    slept: list[float] = []
    _, rec = asyncio.run(_client(fake, slept).generate("hi", ledger=Ledger(), purpose="agent"))
    assert fake.calls == [MAIN, MAIN]
    assert rec.served_model == MAIN and rec.fallback_reason is None
    assert rec.cost_usd == pytest.approx(1000 / 1e6 * 1.50 + 250 / 1e6 * 7.50)


def test_bad_requests_are_not_retried() -> None:
    bad = errors.ClientError(400, {"error": {"code": 400, "message": "bad", "status": "INVALID"}})
    fake = Fake({MAIN: [bad], FAST: []})
    with pytest.raises(errors.ClientError):
        asyncio.run(_client(fake, []).generate("hi", ledger=Ledger(), purpose="agent"))
    assert fake.calls == [MAIN]


def test_both_models_failing_raises_a_clear_error() -> None:
    body = {"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}}
    srv = errors.ServerError(503, body)
    fake = Fake({MAIN: [_429()] * 3, FAST: [srv] * 3})
    with pytest.raises(GeminiError, match="gemini-3.7-flash: 503 UNAVAILABLE after 3 attempts"):
        asyncio.run(_client(fake, []).generate("hi", ledger=Ledger(), purpose="agent"))


def test_token_budget_stops_the_run() -> None:
    fake = Fake({MAIN: [], FAST: []})
    c = _client(fake, [])
    ledger = Ledger(max_tokens=1500)
    asyncio.run(c.generate("hi", ledger=ledger, purpose="agent"))  # 1,250 tokens
    asyncio.run(c.generate("hi", ledger=ledger, purpose="agent"))  # 2,500 tokens
    with pytest.raises(TokenBudgetExceeded):
        asyncio.run(c.generate("hi", ledger=ledger, purpose="agent"))


def test_prices_come_from_the_dated_price_list() -> None:
    assert pricing.price(FAST, date(2026, 12, 31)).input_usd_per_m == 0.75
    assert pricing.price(FAST, date(2027, 1, 1)).output_usd_per_m == 7.50
    assert pricing.cost_usd("gemini-unknown", 1, 1, date(2026, 11, 26)) is None
