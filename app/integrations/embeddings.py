"""OpenAI embeddings client used for both ingestion and query time.

Ingestion and retrieval MUST use the same model: vectors from different
models are not comparable, and mixing them silently degrades results.
"""

from functools import lru_cache

import openai as openai_sdk
from openai import AsyncOpenAI

from app.config import Settings, get_settings
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.core.metrics import (
    ERRORS_TOTAL,
    OPENAI_REQUESTS_TOTAL,
    OPENAI_TOKENS_TOTAL,
)
from app.core.retry import openai_retry

logger = get_logger(__name__)


class EmbeddingClient:
    """Batched embedding generation with retries and Prometheus metrics."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # tenacity owns retries; see app/core/retry.py.
        self._client = AsyncOpenAI(api_key=settings.openai_api_key, max_retries=0)

    @property
    def model(self) -> str:
        return self._settings.embedding_model

    @openai_retry()
    async def _create(self, inputs: list[str]):
        """Single embeddings API attempt."""
        return await self._client.embeddings.create(
            model=self._settings.embedding_model, input=inputs
        )

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed many texts, preserving input order.

        Requests are batched: one HTTP call per ``embedding_batch_size`` items
        instead of one per chunk, which is dramatically faster for a full
        catalogue and stays well inside the per-request token limit.
        """
        if not texts:
            return []

        batch_size = max(1, self._settings.embedding_batch_size)
        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            try:
                response = await self._create(batch)
            except openai_sdk.OpenAIError as exc:
                OPENAI_REQUESTS_TOTAL.labels(model=self.model, status="error").inc()
                ERRORS_TOTAL.labels(type="openai_embeddings").inc()
                logger.error("embedding_request_failed", error=str(exc))
                raise ExternalServiceError("Embedding request failed") from exc

            OPENAI_REQUESTS_TOTAL.labels(model=self.model, status="success").inc()
            usage = getattr(response, "usage", None)
            prompt_tokens = getattr(usage, "prompt_tokens", None)
            if prompt_tokens:
                OPENAI_TOKENS_TOTAL.labels(model=self.model, kind="embedding").inc(
                    prompt_tokens
                )
            # The API may return items out of order; `index` is authoritative.
            ordered = sorted(response.data, key=lambda item: item.index)
            vectors.extend(list(item.embedding) for item in ordered)

        return vectors

    async def embed_query(self, text: str) -> list[float]:
        """Embed a single search query."""
        vectors = await self.embed_texts([text])
        return vectors[0] if vectors else []


@lru_cache
def get_embedding_client() -> EmbeddingClient:
    """Singleton embedding client (shared HTTP connection pool)."""
    return EmbeddingClient(get_settings())
