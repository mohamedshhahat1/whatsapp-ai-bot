"""OpenAI integration using the Responses API, with transient-failure retries."""

import time
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.config import Settings
from app.core.logging import get_logger
from app.core.retry import openai_retry

logger = get_logger(__name__)


@dataclass
class AIResult:
    """Result of one AI generation, including usage data for logging."""

    text: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    latency_ms: int


class OpenAIClient:
    """Tool-calling-ready client over the OpenAI Responses API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # SDK retries disabled: tenacity owns the retry policy (single source
        # of truth, consistent logging, no multiplied retry storms).
        self._client = AsyncOpenAI(api_key=settings.openai_api_key, max_retries=0)

    async def generate_reply(
        self, history: list[dict[str, str]], instructions: str | None = None
    ) -> AIResult:
        """Generate an assistant reply for the given conversation history."""
        started = time.perf_counter()
        response = await self._create_response(history, instructions)
        latency_ms = int((time.perf_counter() - started) * 1000)
        usage = getattr(response, "usage", None)
        return AIResult(
            text=response.output_text,
            model=getattr(response, "model", self._settings.openai_model),
            prompt_tokens=getattr(usage, "input_tokens", None),
            completion_tokens=getattr(usage, "output_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
            latency_ms=latency_ms,
        )

    @openai_retry()
    async def _create_response(
        self, history: list[dict[str, str]], instructions: str | None
    ):
        """Single Responses API attempt; tenacity retries transient failures."""
        # Tool calling: pass `tools=[...]` here when adding function calling.
        return await self._client.responses.create(
            model=self._settings.openai_model,
            instructions=instructions or self._settings.system_prompt,
            input=history,
            max_output_tokens=self._settings.max_output_tokens,
        )
