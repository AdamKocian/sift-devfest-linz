"""The one Gemini client. Every LLM call in Sift (the ADK agent, Ask Sift, the usage
resolver) goes through `GeminiClient.generate`.

- Vertex AI only (`genai.Client(vertexai=True, ...)`), project from GOOGLE_CLOUD_PROJECT,
  location `global` by default (the global endpoint "can improve overall availability and
  reduce resource exhausted (429) errors":
  https://cloud.google.com/vertex-ai/generative-ai/docs/learn/locations#global-endpoint).
- Model ids only from GEMINI_MODEL / GEMINI_FAST_MODEL (see config.py).
- Retries: truncated exponential backoff with jitter on 408/429/5xx and network errors, at
  most 3 attempts and about 20 s per model, as the retry guide recommends
  (https://cloud.google.com/vertex-ai/generative-ai/docs/retry-strategy). The SDK's own
  retries are switched off (`HttpRetryOptions(attempts=1)`, "0 or 1 means no retries") so
  this is the only retry policy.
- Fallback: when the requested model is still failing, the call goes to the fast model.
  The rest of the run then uses the fast model directly, and the requested model is skipped
  by every run for a few minutes, so one 429 does not cost 20 s per call.
- Every call is recorded (requested and serving model, attempts, tokens, estimated cost)
  in the run's `Ledger`; a run stops at its token budget."""

from __future__ import annotations

import asyncio
import random
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from google.genai import errors, types

from server.agent import pricing

RETRYABLE = frozenset({408, 429, 500, 502, 503, 504})
# The model id does not exist for this project or region: no point retrying it.
FALLBACK_ONLY = frozenset({404})
NETWORK = 0  # Attempt.code for timeouts and dropped connections

Transport = Callable[
    [str, Any, types.GenerateContentConfig | None], Awaitable[types.GenerateContentResponse]
]


class GeminiError(RuntimeError):
    """A Gemini call failed on every model it was allowed to use."""


class TokenBudgetExceeded(GeminiError):
    pass


@dataclass
class Attempt:
    model: str
    code: int | None  # None = success, 0 = network error, else HTTP status
    status: str | None = None
    wait_s: float = 0.0  # backoff before the next attempt
    message: str = ""


@dataclass
class CallRecord:
    purpose: str  # agent | chat | resolver
    requested_model: str
    served_model: str
    tokens_in: int
    tokens_out: int  # response + thinking (both billed as output)
    thoughts: int
    cost_usd: float | None
    duration_ms: int
    attempts: list[Attempt] = field(default_factory=list)
    fallback_reason: str | None = None
    cached: int = 0  # part of tokens_in served from Vertex AI's implicit cache

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> CallRecord:
        d = dict(d)
        d["attempts"] = [Attempt(**a) for a in d.get("attempts", [])]
        return cls(**d)


@dataclass
class Ledger:
    """Gemini usage of one run (or one chat answer)."""

    max_tokens: int = 400_000
    calls: list[CallRecord] = field(default_factory=list)
    sticky_model: str | None = None  # after a fallback, the rest of the run uses this
    on_call: Callable[[CallRecord], None] | None = None

    @property
    def tokens_in(self) -> int:
        return sum(c.tokens_in for c in self.calls)

    @property
    def tokens_out(self) -> int:
        return sum(c.tokens_out for c in self.calls)

    @property
    def total(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def cost_usd(self) -> float | None:
        known = [c.cost_usd for c in self.calls if c.cost_usd is not None]
        return round(sum(known), 6) if known else None

    @property
    def requested_model(self) -> str | None:
        return self.calls[0].requested_model if self.calls else None

    @property
    def served_model(self) -> str | None:
        """The model that served most calls (ties: the later one)."""
        if not self.calls:
            return None
        counts = Counter(c.served_model for c in self.calls)
        return max(reversed([c.served_model for c in self.calls]), key=lambda m: counts[m])

    @property
    def fallback_reason(self) -> str | None:
        return next((c.fallback_reason for c in self.calls if c.fallback_reason), None)

    def add(self, rec: CallRecord) -> None:
        self.calls.append(rec)
        if self.on_call:
            self.on_call(rec)

    def check_budget(self) -> None:
        if self.total >= self.max_tokens:
            raise TokenBudgetExceeded(
                f"token budget of {self.max_tokens:,} per run used up ({self.total:,} tokens)"
            )


def usage_tokens(resp: types.GenerateContentResponse) -> tuple[int, int, int, int]:
    """(input, output incl. thinking, thinking, cached input) from usage_metadata."""
    u = resp.usage_metadata
    if u is None:
        return 0, 0, 0, 0
    tokens_in = (u.prompt_token_count or 0) + (u.tool_use_prompt_token_count or 0)
    thoughts = u.thoughts_token_count or 0
    cached = u.cached_content_token_count or 0
    return tokens_in, (u.candidates_token_count or 0) + thoughts, thoughts, cached


def vertex_transport(project: str, location: str = "global") -> Transport:
    """google-genai on Vertex AI with the SDK's retries off (we retry ourselves)."""
    from google import genai

    client = genai.Client(
        vertexai=True,
        project=project,
        location=location,
        http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
    )

    async def call(
        model: str, contents: Any, config: types.GenerateContentConfig | None
    ) -> types.GenerateContentResponse:
        return await client.aio.models.generate_content(
            model=model, contents=contents, config=config
        )

    return call


def _network_errors() -> tuple[type[BaseException], ...]:
    import httpx

    out: list[type[BaseException]] = [httpx.TransportError, asyncio.TimeoutError, ConnectionError]
    try:
        import aiohttp

        out.append(aiohttp.ClientError)
    except ImportError:  # pragma: no cover
        pass
    return tuple(out)


class GeminiClient:
    # Shared by all clients in the process: a model that just failed is skipped for a while.
    _cooldown: dict[str, float] = {}

    def __init__(
        self,
        model: str,
        fast_model: str,
        transport: Transport,
        *,
        max_attempts: int = 3,
        base_delay_s: float = 3.0,
        max_delay_s: float = 10.0,
        retry_budget_s: float = 20.0,
        cooldown_s: float = 300.0,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], date] = date.today,
    ) -> None:
        if not model or not fast_model:
            raise ValueError("GEMINI_MODEL and GEMINI_FAST_MODEL must both be set")
        self.model, self.fast_model, self.transport = model, fast_model, transport
        self.max_attempts, self.base_delay_s = max_attempts, base_delay_s
        self.max_delay_s, self.retry_budget_s, self.cooldown_s = (
            max_delay_s,
            retry_budget_s,
            cooldown_s,
        )
        self.sleep, self.jitter, self.clock, self.today = sleep, jitter, clock, today
        self._net = _network_errors()

    @classmethod
    def vertex(cls, project: str, location: str, model: str, fast_model: str) -> GeminiClient:
        if not project:
            raise ValueError("GOOGLE_CLOUD_PROJECT must be set for Gemini on Vertex AI")
        return cls(model, fast_model, vertex_transport(project, location or "global"))

    def _backoff(self, n: int) -> float:
        """Truncated exponential backoff: 3 s, 6 s, ... capped at 10 s, ±25 % jitter."""
        return min(self.max_delay_s, self.base_delay_s * 2**n) * (0.75 + 0.5 * self.jitter())

    async def _try(
        self, model: str, contents: Any, config: types.GenerateContentConfig | None
    ) -> tuple[types.GenerateContentResponse | None, list[Attempt]]:
        attempts: list[Attempt] = []
        started = self.clock()
        for n in range(self.max_attempts):
            try:
                resp = await self.transport(model, contents, config)
            except errors.APIError as e:
                code, status, msg = e.code, e.status, str(e.message or "")[:200]
                if code not in RETRYABLE and code not in FALLBACK_ONLY:
                    raise
            except self._net as e:
                code, status, msg = NETWORK, "NETWORK", f"{type(e).__name__}: {e}"[:200]
            else:
                attempts.append(Attempt(model, None))
                return resp, attempts
            wait = self._backoff(n)
            last = (
                n == self.max_attempts - 1
                or code in FALLBACK_ONLY
                or self.clock() - started + wait > self.retry_budget_s
            )
            attempts.append(Attempt(model, code, status, 0.0 if last else round(wait, 2), msg))
            if last:
                return None, attempts
            await self.sleep(wait)
        return None, attempts  # pragma: no cover

    def _skip_reason(self, model: str, ledger: Ledger) -> str | None:
        if ledger.sticky_model and ledger.sticky_model != model:
            return f"{model} failed earlier in this run"
        until = self._cooldown.get(model)
        if until is not None and self.clock() < until:
            return f"{model} failed in the last {self.cooldown_s / 60:.0f} min"
        return None

    async def generate(
        self,
        contents: Any,
        config: types.GenerateContentConfig | None = None,
        *,
        ledger: Ledger,
        purpose: str,
        model: str | None = None,
    ) -> tuple[types.GenerateContentResponse, CallRecord]:
        ledger.check_budget()
        requested = model or self.model
        order = [requested] if requested == self.fast_model else [requested, self.fast_model]
        reason: str | None = None
        if len(order) > 1 and (skip := self._skip_reason(requested, ledger)):
            order, reason = order[1:], f"skipped: {skip}"
        t0 = time.monotonic()
        tried: list[Attempt] = []
        for i, m in enumerate(order):
            resp, attempts = await self._try(m, contents, config)
            tried.extend(attempts)
            if resp is not None:
                tokens_in, tokens_out, thoughts, cached = usage_tokens(resp)
                served = resp.model_version or m
                rec = CallRecord(
                    purpose=purpose,
                    requested_model=requested,
                    served_model=served,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    thoughts=thoughts,
                    cost_usd=pricing.cost_usd(
                        served, tokens_in, tokens_out, self.today(), cached=cached
                    ),
                    duration_ms=round((time.monotonic() - t0) * 1000),
                    attempts=tried,
                    fallback_reason=reason,
                    cached=cached,
                )
                ledger.add(rec)
                return resp, rec
            bad = attempts[-1]
            if i == len(order) - 1:
                raise GeminiError(
                    f"{m}: {bad.code} {bad.status} after {len(attempts)} attempts: {bad.message}"
                )
            reason = f"{bad.code} {bad.status} on {m} after {len(attempts)} attempts"
            ledger.sticky_model = order[i + 1]
            self._cooldown[m] = self.clock() + self.cooldown_s
        raise GeminiError("no model to call")  # pragma: no cover
