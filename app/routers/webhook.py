"""WhatsApp Cloud API webhook: verification and event ingestion.

Inbound deliveries are ACKed immediately and processed asynchronously:
- production: enqueued to Celery (durable, survives crashes, retried)
- development fallback (USE_TASK_QUEUE=false): FastAPI BackgroundTasks

These endpoints are limited as a single shared bucket, not per IP: every
delivery comes from Meta, so an IP key would throttle the whole customer base
together. Per-customer limits live in ``app/core/quota.py`` and run inside the
worker, once the payload has been parsed and the sender is known.

Unsigned deliveries are accepted ONLY outside production, and only when no app
secret is configured. See ``verify_meta_signature``.
"""

import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.config import get_settings
from app.core.logging import get_logger
from app.core.ratelimit import WEBHOOK_LIMIT, limiter, webhook_key
from app.core.security import verify_meta_signature, verify_token_matches
from app.db.session import SessionLocal
from app.dependencies.deps import get_openai_client, get_whatsapp_client
from app.services.webhook_processor import process_webhook_payload
from app.workers.tasks import process_webhook_event

logger = get_logger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])


@router.get("")
@limiter.limit(WEBHOOK_LIMIT, key_func=webhook_key)
async def verify_webhook(request: Request) -> PlainTextResponse:
    """Meta webhook verification handshake (hub.challenge echo)."""
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and verify_token_matches(
        get_settings().whatsapp_verify_token, params.get("hub.verify_token")
    ):
        return PlainTextResponse(params.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("")
@limiter.limit(WEBHOOK_LIMIT, key_func=webhook_key)
async def receive_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> dict[str, str]:
    """Validate the Meta signature, ACK fast, and enqueue processing.

    The ACK is unconditional once the signature checks out. Meta retries any
    delivery it does not see acknowledged within a few seconds, so doing real
    work here would turn one slow completion into several duplicate
    deliveries -- which the idempotency guards would then have to absorb.

    ``allow_unsigned`` is granted only outside production. A production stack
    with an empty app secret now rejects every delivery, which is loud and
    fixable; the alternative was accepting forged ones in silence.
    """
    settings = get_settings()
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_meta_signature(
        settings.whatsapp_app_secret,
        raw_body,
        signature,
        allow_unsigned=settings.environment != "production",
    ):
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    if settings.use_task_queue:
        process_webhook_event.delay(payload)
    else:
        background_tasks.add_task(_process_inline, payload)
    return {"status": "queued"}


async def _process_inline(payload: dict[str, Any]) -> None:
    """In-process fallback used when the task queue is disabled (dev only)."""
    try:
        async with SessionLocal() as session:
            await process_webhook_payload(
                session,
                get_whatsapp_client(),
                get_openai_client(),
                get_settings(),
                payload,
            )
    except Exception:
        logger.error("inline_processing_failed", exc_info=True)
