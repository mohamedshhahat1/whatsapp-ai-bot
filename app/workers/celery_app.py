"""Celery application configured for durable, at-least-once processing."""

from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "whatsapp_ai_bot",
    broker=settings.broker_url,
    backend=settings.result_backend,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Durability: ack only after the task finishes, so a worker crash
    # mid-processing re-queues the message instead of losing it.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_default_queue="webhooks",
    result_expires=3600,
)
