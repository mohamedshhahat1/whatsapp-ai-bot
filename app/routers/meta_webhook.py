"""Meta webhook for Messenger, and later Instagram and comment surfaces.

A separate route from ``/webhook`` on purpose. Meta subscribes a URL per
product, the two payload shapes share nothing but the envelope, and the
WhatsApp handler is carrying live traffic for a real business -- there is no
version of folding Messenger into it that leaves that path untouched.

What is NOT duplicated is the security. Both helpers come from
``app.core.security`` unchanged; only the credentials differ, and those are
resolved through ``ChannelSettings``, which falls back to the WhatsApp app
secret and verify token so that the usual single-Meta-app setup needs no
second copy of either value.

Like ``/webhook``, this ACKs immediately and does the work elsewhere:

- production: enqueued to Celery (durable, survives crashes, retried)
- development fallback (USE_TASK_QUEUE=false): FastAPI BackgroundTasks

The rate limit is the same shared bucket, for the same reason -- every
delivery comes from Meta, so keying on IP would throttle the entire customer
base as one.
"""

import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.channels.config import get_channel_settings
from app.channels.constants import MESSENGER
from app.channels.messenger import MessengerAdapter
from app.config import get_settings
from app.core.logging import get_logger
from app.core.ratelimit import WEBHOOK_LIMIT, limiter, webhook_key
from app.core.security import verify_meta_signature, verify_token_matches
from app.db.session import SessionLocal
from app.dependencies.deps import get_openai_client
from app.services.webhook_processor import process_meta_payload
from app.workers.tasks import process_meta_webhook_event

logger = get_logger(__name__)
router = APIRouter(prefix="/webhook/meta", tags=["webhook"])


@router.get("")
@limiter.limit(WEBHOOK_LIMIT, key_func=webhook_key)
async def verify_meta_webhook(request: Request) -> PlainTextResponse:
    """Meta webhook verification handshake (hub.challenge echo).

    Answered whether or not the channel is switched on. The handshake proves
    ownership of the endpoint and echoes a value Meta just supplied; it moves
    no customer data. Refusing it while disabled would only force operators to
    enable a channel before they could finish configuring it, which is the
    wrong order.
    """
    params = request.query_params
    settings = get_settings()
    channels = get_channel_settings()
    if params.get("hub.mode") == "subscribe" and verify_token_matches(
        channels.verify_token(settings.whatsapp_verify_token),
        params.get("hub.verify_token"),
    ):
        return PlainTextResponse(params.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("")
@limiter.limit(WEBHOOK_LIMIT, key_func=webhook_key)
async def receive_meta_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> dict[str, str]:
    """Validate the Meta signature, ACK fast, and enqueue processing.

    Three refusals, in this order:

    1. A bad signature is a 403 and nothing else happens. The body is not
       parsed, because parsing unverified input is the thing the signature
       exists to avoid.
    2. Messenger switched off ACKs and drops. Meta keeps delivering to a
       subscribed webhook regardless of what this application thinks, so the
       switch has to be enforced here rather than assumed.
    3. Anything whose ``object`` is not ``page`` ACKs and drops -- Instagram
       and the comment surfaces arrive on this same URL and are not wired
       yet, and apologising to a commenter would be worse than silence.

    The last two answer 200 deliberately. A non-200 tells Meta the delivery
    failed and it will retry the same payload for hours.
    """
    settings = get_settings()
    channels = get_channel_settings()
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_meta_signature(
        channels.app_secret(settings.whatsapp_app_secret),
        raw_body,
        signature,
        allow_unsigned=settings.environment != "production",
    ):
        raise HTTPException(status_code=403, detail="Invalid signature")

    if not channels.switches.get(MESSENGER, False):
        logger.info("meta_webhook_ignored", reason="messenger_disabled")
        return {"status": "ignored"}

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    object_type = payload.get("object")
    if object_type != "page":
        logger.info(
            "meta_webhook_ignored",
            reason="unsupported_object",
            object_type=str(object_type),
        )
        return {"status": "ignored"}

    if settings.use_task_queue:
        process_meta_webhook_event.delay(payload)
    else:
        background_tasks.add_task(_process_inline, payload)
    return {"status": "queued"}


async def _process_inline(payload: dict[str, Any]) -> None:
    """In-process fallback used when the task queue is disabled (dev only).

    The adapter is built per delivery and closed here. Unlike the WhatsApp
    client it is not a cached singleton, because it is only reachable on this
    path and a process-wide client would hold a connection pool open for a
    channel that may never be switched on.
    """
    adapter = MessengerAdapter(get_channel_settings())
    try:
        async with SessionLocal() as session:
            await process_meta_payload(
                session,
                adapter,
                get_openai_client(),
                get_settings(),
                payload,
            )
    except Exception:
        logger.error("inline_processing_failed", exc_info=True)
    finally:
        await adapter.aclose()
