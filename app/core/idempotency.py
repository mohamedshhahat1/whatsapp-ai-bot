"""Make OpenAI generation idempotent across Celery retries.

The database can guarantee that one inbound message produces at most one
outbound row -- ``messages.reply_to_wa_message_id`` is unique. What it cannot
do is stop us paying for the same completion twice.

Consider a worker that calls OpenAI, gets a 400-token answer back, and is then
SIGKILLed by an OOM or a host failure before it writes anything. Postgres has
no record that the call ever happened. The delivery was never acked, so the
broker redelivers it, and the retry generates the same answer again and is
billed again. Repeat that across a bad deploy and the invoice is real money.

So the completion is cached in Redis against the inbound message id the moment
it comes back, before any database work. A retry finds it and skips the API
call entirely.

Redis, not Postgres, on purpose. This value is written on the hot path between
two external calls, it is worthless after a day, and it must survive the
transaction being rolled back -- all three of which argue against a table. If
Redis is unavailable the cache degrades to a miss: the reply is still correct,
it just costs another completion. Losing a customer's reply because a cache
was down would be the worse trade, so nothing here is allowed to raise.
"""

import json
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_PREFIX = "idem:reply:"


@dataclass(frozen=True)
class CachedGeneration:
    """A model completion held against the message that prompted it.

    The token counts ride along so a replayed generation still lands in
    ``ai_logs`` with the usage that was actually billed. Logging zeros on the
    retry would quietly understate spend in the analytics -- the money left
    the account on the first attempt whether or not that attempt finished.
    """

    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: int | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "text": self.text,
                "model": self.model,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "latency_ms": self.latency_ms,
            }
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "CachedGeneration | None":
        try:
            data: dict[str, Any] = json.loads(raw)
            return cls(
                text=str(data["text"]),
                model=str(data.get("model", "")),
                prompt_tokens=int(data.get("prompt_tokens") or 0),
                completion_tokens=int(data.get("completion_tokens") or 0),
                total_tokens=int(data.get("total_tokens") or 0),
                latency_ms=data.get("latency_ms"),
            )
        except Exception:
            # A malformed entry (an old schema, a truncated write) must not
            # take the conversation down. Treat it as a miss and regenerate.
            logger.warning("idempotency_cache_unreadable", exc_info=True)
            return None


def _key(wa_message_id: str) -> str:
    return f"{_PREFIX}{wa_message_id}"


async def get_cached_generation(
    wa_message_id: str, settings: Settings
) -> CachedGeneration | None:
    """Return the completion already generated for this inbound message.

    ``None`` means "generate it" -- either nothing is cached, or the cache is
    unreachable. Both are safe: the worst case is paying for one completion
    twice, which is strictly better than not answering the customer.
    """
    if not wa_message_id:
        return None
    try:
        from redis.asyncio import Redis

        client = Redis.from_url(settings.redis_url)
        try:
            raw = await client.get(_key(wa_message_id))
        finally:
            await client.aclose()
    except Exception as exc:
        logger.warning("idempotency_cache_read_failed", error=str(exc))
        return None

    if raw is None:
        return None

    cached = CachedGeneration.from_json(raw)
    if cached is not None:
        logger.info("generation_replayed_from_cache", wa_message_id=wa_message_id)
    return cached


async def store_generation(
    wa_message_id: str, generation: CachedGeneration, settings: Settings
) -> None:
    """Cache a completion so a retry of this delivery does not pay for it again.

    Called immediately after the API returns and before any database write,
    because the window this closes is precisely the one where the database
    write never happens.

    Failures are swallowed. A cache we could not write means the next attempt
    regenerates, which costs a fraction of a cent -- raising here would fail a
    delivery whose expensive part has already succeeded.
    """
    if not wa_message_id:
        return
    try:
        from redis.asyncio import Redis

        client = Redis.from_url(settings.redis_url)
        try:
            await client.set(
                _key(wa_message_id),
                generation.to_json(),
                ex=settings.reply_idempotency_ttl_seconds,
            )
        finally:
            await client.aclose()
    except Exception as exc:
        logger.warning("idempotency_cache_write_failed", error=str(exc))


async def clear_generation(wa_message_id: str, settings: Settings) -> None:
    """Drop a cached completion once its reply is durably recorded.

    Not required for correctness -- the TTL would expire it anyway -- but it
    keeps Redis proportional to in-flight work rather than to a day of
    traffic, and it means a replay after a successful send cannot resurrect a
    stale answer if the row is later deleted by an operator.
    """
    if not wa_message_id:
        return
    try:
        from redis.asyncio import Redis

        client = Redis.from_url(settings.redis_url)
        try:
            await client.delete(_key(wa_message_id))
        finally:
            await client.aclose()
    except Exception:
        # Nothing depends on this succeeding.
        pass
