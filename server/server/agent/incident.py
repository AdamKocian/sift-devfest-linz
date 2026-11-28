"""The incident note after a failed verification, written by Gemini (GEMINI_FAST_MODEL)
through the one Gemini client. Only with SIFT_AGENT=gemini; the caller number-checks the
text against the check results and falls back to the template (server.verify).

Not run yet: GCP billing on the project is disabled since 2026-11-27, so this path has only
been exercised with a fake writer in tests."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from collections.abc import Callable
from typing import Any

from google.genai import types

from server.agent.gemini import GeminiClient, Ledger
from server.config import Config

PROMPT = """\
A telemetry rule was applied in Dynatrace, then verified, and the verification failed, so
Sift reverted it. Write the incident note for the person who approved it: 3 short plain
sentences: what failed, the evidence, the next step. Use only numbers that appear below.
No greeting, no markdown.

{facts}
"""


def gemini_writer(cfg: Config) -> Callable[[dict[str, Any]], str]:
    client = GeminiClient.vertex(
        cfg.gcp_project, cfg.gcp_location, cfg.gemini_model, cfg.gemini_fast_model
    )

    async def ask(facts: dict[str, Any]) -> str:
        resp, _ = await client.generate(
            PROMPT.format(facts=json.dumps(facts, ensure_ascii=False, indent=1)),
            types.GenerateContentConfig(
                max_output_tokens=400,
                thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
            ),
            ledger=Ledger(max_tokens=20_000),
            purpose="incident_note",
            model=client.fast_model,
        )
        return resp.text or ""

    def write(facts: dict[str, Any]) -> str:
        # Called from synchronous code that may run inside the server's event loop.
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            return pool.submit(asyncio.run, ask(facts)).result(timeout=60)

    return write
