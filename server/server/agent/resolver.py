"""The usage resolver on Gemini: queries the deterministic DQL parser cannot map to signals
(a dashboard variable whose values come from another query, an unknown function) go to
GEMINI_FAST_MODEL through the one Gemini client. Answers are cached by query text
(`usage.query_key`), in the same shape as fixtures/agent/usage_resolutions.json, so fixture
mode replays them with `RecordedResolver`."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from typing import Any

from google.genai import types
from pydantic import BaseModel, Field

from server.agent.gemini import GeminiClient, Ledger
from server.engines.discover import SignalDraft
from server.engines.usage import Resolution, Site, query_key

PROMPT = """\
Sift's DQL parser could not tell which telemetry this Dynatrace query reads.
Say which of the candidate signals it reads and which fields it uses (filters, group-by,
fields it returns). Use only signal ids from the list. If a dashboard variable is used,
assume it can take any of the values listed for it.

Query ({kind} "{name}"):
{dql}

Why the parser gave up: {reason}
Dashboard variables: {variables}

Candidate signals (id | kind | service | key):
{catalog}
"""


class _Answer(BaseModel):
    signal_ids: list[str] = Field(description="ids of the signals the query reads")
    fields: list[str] = Field(description="fields the query uses")
    note: str = Field(description="one sentence: why")


class GeminiResolver:
    def __init__(
        self, gemini: GeminiClient, ledger: Ledger, cache: dict[str, dict[str, Any]] | None = None
    ) -> None:
        self.gemini, self.ledger = gemini, ledger
        self.cache: dict[str, dict[str, Any]] = cache if cache is not None else {}

    def _prompt(self, site: Site, reason: str, catalog: list[SignalDraft]) -> str:
        # The candidates: signals of the services the query names, or all if none match.
        named = [s for s in catalog if s.service in site.dql] or catalog
        rows = "\n".join(f"{s.id} | {s.kind} | {s.service} | {s.key}" for s in named[:80])
        variables = {k: list(v.values) for k, v in site.variables.items()} or "none"
        return PROMPT.format(
            kind=site.type,
            name=site.name + (f" / {site.part}" if site.part else ""),
            dql=site.dql,
            reason=reason,
            variables=json.dumps(variables),
            catalog=rows,
        )

    async def _ask(self, prompt: str) -> _Answer:
        resp, _ = await self.gemini.generate(
            prompt,
            types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_Answer,
                max_output_tokens=2048,
                thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
            ),
            ledger=self.ledger,
            purpose="resolver",
            model=self.gemini.fast_model,
        )
        return _Answer.model_validate_json(resp.text or "{}")

    def resolve(self, site: Site, reason: str, catalog: list[SignalDraft]) -> Resolution | None:
        key = query_key(site.dql)
        if key not in self.cache:
            prompt = self._prompt(site, reason, catalog)
            # The discovery pipeline is synchronous and may run inside the server's event
            # loop, so the call gets its own loop in a worker thread.
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                answer = pool.submit(asyncio.run, self._ask(prompt)).result()
            served = self.ledger.calls[-1].served_model if self.ledger.calls else "gemini"
            self.cache[key] = {
                "site": f"{site.name} / {site.part}" if site.part else site.name,
                "query": site.dql,
                "signal_ids": answer.signal_ids,
                "fields": answer.fields,
                "note": answer.note,
                "model": served,
            }
        a = self.cache[key]
        known = {s.id for s in catalog}
        ids = tuple(s for s in a["signal_ids"] if s in known)
        if not ids:
            return None
        return Resolution(ids, tuple(a["fields"]), a["note"], a["model"])
