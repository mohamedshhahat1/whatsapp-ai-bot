"""OpenAI integration using the Responses API, with retries and metrics."""

import time
from dataclasses import dataclass
from typing import Any, cast

import openai as openai_sdk
from openai import AsyncOpenAI

from app.config import Settings
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.core.metrics import (
    ERRORS_TOTAL,
    OPENAI_REQUESTS_TOTAL,
    OPENAI_RESPONSE_SECONDS,
    OPENAI_TOKENS_TOTAL,
)
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

    async def aclose(self) -> None:
        """Release the underlying HTTP connection pool.

        Every Celery task builds a client inside its own event loop, so the
        pool has to be closed with the loop that opened it. Without this the
        worker leaks a connection pool and its sockets per task.
        """
        await self._client.close()

    async def generate_reply(
        self, history: list[dict[str, str]], instructions: str | None = None
    ) -> AIResult:
        """Generate an assistant reply for the given conversation history."""
        started = time.perf_counter()
        try:
            response = await self._create_response(history, instructions)
        except openai_sdk.OpenAIError as exc:
            OPENAI_REQUESTS_TOTAL.labels(
                model=self._settings.openai_model, status="error"
            ).inc()
            ERRORS_TOTAL.labels(type="openai").inc()
            logger.error("openai_request_failed", error=str(exc))
            raise ExternalServiceError("OpenAI request failed") from exc
        elapsed = time.perf_counter() - started

        model = getattr(response, "model", self._settings.openai_model)
        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "input_tokens", None)
        completion_tokens = getattr(usage, "output_tokens", None)

        OPENAI_REQUESTS_TOTAL.labels(model=model, status="success").inc()
        OPENAI_RESPONSE_SECONDS.labels(model=model).observe(elapsed)
        if prompt_tokens:
            OPENAI_TOKENS_TOTAL.labels(model=model, kind="prompt").inc(prompt_tokens)
        if completion_tokens:
            OPENAI_TOKENS_TOTAL.labels(model=model, kind="completion").inc(
                completion_tokens
            )

        # Spend is deliberately NOT computed here. Token counts are a fact the
        # API reports; converting them to money needs the price that was in
        # force at the time, which lives in the model_pricing table. Doing it
        # twice -- once from Settings for a metric, once from model_pricing for
        # the dashboard -- produced two numbers that disagreed. The single
        # source of truth is AnalyticsRepository; see docs/PRICING.md.

        return AIResult(
            text=response.output_text,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=getattr(usage, "total_tokens", None),
            latency_ms=int(elapsed * 1000),
        )

    @openai_retry()
    async def _create_response(
        self, history: list[dict[str, str]], instructions: str | None
    ):
        """Single Responses API attempt; tenacity retries transient failures."""
        # History is a list of {"role": ..., "content": ...} dicts, which is
        # structurally an EasyInputMessageParam. The SDK types `input` as a
        # large TypedDict union that plain dicts do not statically satisfy,
        # so cast at the boundary instead of importing 30+ param types.
        # Tool calling: pass `tools=[...]` here when adding function calling.
        return await self._client.responses.create(
            model=self._settings.openai_model,
            instructions=instructions or self._settings.system_prompt,
            input=cast(Any, history),
            max_output_tokens=self._settings.max_output_tokens,
        )
