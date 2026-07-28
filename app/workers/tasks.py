"""Celery tasks.

Each task runs its own event loop (``asyncio.run``), so all async resources
(asyncpg connections, httpx clients) are created inside that loop rather than
shared across tasks -- sharing them across event loops is unsafe. The flip
side is that every one of them must also be closed before the loop ends,
which is what the ``finally`` block below is for.
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import redis
from celery import Task
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.metrics import WEBHOOK_DEAD_LETTERS_TOTAL
from app.integrations.openai import OpenAIClient
from app.integrations.whatsapp import WhatsAppClient
from app.services.webhook_processor import process_webhook_payload
from app.workers.celery_app import celery_app

settings = get_settings()
configure_logging(debug=settings.debug)
logger = get_logger(__name__)

MAX_RETRIES = 5


async def _run(payload: dict[str, Any]) -> None:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    whatsapp = WhatsAppClient(settings)
    ai = OpenAIClient(settings)
    try:
        async with session_factory() as session:
            await process_webhook_payload(session, whatsapp, ai, settings, payload)
    finally:
        # Every client opened in this loop is closed in this loop, in reverse
        # order of creation. Closing is best-effort: a failure here must not
        # mask the original exception, or a retriable error would surface as
        # a shutdown error and lose its traceback.
        for label, close in (
            ("openai", ai.aclose),
            ("whatsapp", whatsapp.aclose),
            ("engine", engine.dispose),
        ):
            try:
                await close()
            except Exception:
                logger.warning("resource_close_failed", resource=label, exc_info=True)


def _dead_letter(payload: dict[str, Any], error: BaseException) -> None:
    """Park a delivery that exhausted its retries.

    Celery's default behaviour after the last retry is to record a failure in
    the result backend, which expires after an hour -- the customer's message
    is then gone with no trace. Pushing the raw payload onto a capped Redis
    list means it can be inspected and replayed, and the counter gives
    Prometheus something to alert on.
    """
    entry = json.dumps(
        {
            "failed_at": datetime.now(UTC).isoformat(),
            "error": f"{type(error).__name__}: {error}",
            "payload": payload,
        }
    )
    try:
        with redis.Redis.from_url(settings.redis_url) as client:
            client.lpush(settings.dead_letter_key, entry)
            client.ltrim(settings.dead_letter_key, 0, settings.dead_letter_max_entries - 1)
    except Exception:
        # Losing the dead letter is bad, but raising here would replace the
        # real error with a Redis error in the logs.
        logger.error("dead_letter_write_failed", exc_info=True)


@celery_app.task(
    bind=True,
    name="webhooks.process_event",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=MAX_RETRIES,
    retry_kwargs={"max_retries": MAX_RETRIES},
)
def process_webhook_event(self: Task, payload: dict[str, Any]) -> None:
    """Durably process one webhook delivery with exponential-backoff retries."""
    try:
        asyncio.run(_run(payload))
    except Exception as exc:
        if self.request.retries >= MAX_RETRIES:
            WEBHOOK_DEAD_LETTERS_TOTAL.inc()
            logger.error(
                "webhook_dead_lettered",
                retries=self.request.retries,
                error=str(exc),
            )
            _dead_letter(payload, exc)
        raise
