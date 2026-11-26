"""ADK model adapter: the agent's model turns go through Sift's one Gemini client, so the
backoff, the fallback to the fast model and the usage ledger apply to the agent too.

ADK lets a custom model subclass `google.adk.models.base_llm.BaseLlm` and implement
`generate_content_async(llm_request, stream)`, yielding `LlmResponse`
(google/adk/models/base_llm.py, adk-python 2.15.1). The built-in `Gemini` model builds its
own genai client; this one does not."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models._capabilities import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from pydantic import Field

from server.agent.gemini import GeminiClient, Ledger


class SiftGemini(BaseLlm):
    """`model` is the requested model (GEMINI_MODEL); the ledger says which one served."""

    gemini: Any = Field(exclude=True)  # GeminiClient
    ledger: Any = Field(exclude=True)  # Ledger
    purpose: str = "agent"

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=False)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self._maybe_append_user_content(llm_request)
        client: GeminiClient = self.gemini
        ledger: Ledger = self.ledger
        resp, rec = await client.generate(
            llm_request.contents,
            llm_request.config,
            ledger=ledger,
            purpose=self.purpose,
            model=self.model,
        )
        out = LlmResponse.create(resp)
        out.custom_metadata = {"served_model": rec.served_model, "call": len(ledger.calls) - 1}
        yield out
