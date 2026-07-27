"""Celery tasks.

Each task runs its own event loop (``asyncio.run``), so all async resources
(asyncpg connections, httpx clients) are created inside that loop rather than
shared across tasks — sharing them across event loops is unsafe.
"""

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.integrations.openai import OpenAIClient
from app.integrations.whatsapp import WhatsAppClient
from app.services.webhook_processor import process_webhook_payload
from app.workers.celery_app import celery_app

settings = get_settings()
configure_logging(debug=settings.debug)
logger = get_logger(__name__)


async def _run(payload: dict[str, Any]) -> None:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    whatsapp = WhatsAppClient(settings)
    ai = OpenAIClient(settings)
    try:
        async with session_factory() as session:
            await process_webhook_payload(session, whatsapp, ai, settings, payload)
    finally:
        await whatsapp.aclose()
        await engine.dispose()


@celery_app.task(
    name="webhooks.process_event",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
)
def process_webhook_event(payload: dict[str, Any]) -> None:
    """Durably process one webhook delivery with exponential-backoff retries."""
    asyncio.run(_run(payload))
