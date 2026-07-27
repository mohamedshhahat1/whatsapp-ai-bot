"""OpenAI client built on the Responses API (no deprecated endpoints)."""

import time
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from app.config import Settings
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class AIResult:
    """Normalized result of a single model call."""

    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: int


class OpenAIClient:
    """Async wrapper around the OpenAI Responses API.

    ``tools`` can be passed to make the assistant function-calling ready;
    handling of tool call outputs can be layered on in the service layer.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def generate_reply(
        self,
        history: list[dict[str, Any]],
        instructions: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> AIResult:
        """Generate an assistant reply for the given conversation history.

        ``history`` is a list of ``{"role": "user"|"assistant", "content": str}``.
        """
        start = time.perf_counter()
        try:
            response = await self._client.responses.create(
                model=self._settings.openai_model,
                instructions=instructions or self._settings.system_prompt,
                input=history,
                max_output_tokens=self._settings.max_output_tokens,
                tools=tools or [],
            )
        except Exception as exc:
            logger.error("openai_request_failed", error=str(exc))
            raise ExternalServiceError("AI provider request failed") from exc

        latency_ms = int((time.perf_counter() - start) * 1000)
        usage = response.usage
        return AIResult(
            text=response.output_text or "",
            model=response.model,
            prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(usage, "output_tokens", 0) or 0,
            total_tokens=getattr(usage, "total_tokens", 0) or 0,
            latency_ms=latency_ms,
        )
