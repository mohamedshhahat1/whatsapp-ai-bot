"""Meta webhook for Messenger and Instagram, and later the comment surfaces.

A separate route from ``/webhook`` on purpose. Meta subscribes a URL per
product, the two payload shapes share nothing but the envelope, and the
WhatsApp handler is carrying live traffic for a real business -- there is no
version of folding Messenger into it that leaves that path untouched.

What is NOT duplicated is the security. Both helpers come from
``app.core.security`` unchanged; only the credentials differ, and those are
resolved through ``ChannelSettings``, which falls back to the WhatsApp app
secret and verify token so that the usual single-Meta-app setup needs no
second copy of either value.

One URL serves every Meta surface, and the envelope's ``object`` field is the
only thing that says which. That lookup lives in the registry rather than here
so the route holds no channel knowledge of its own: ``page`` is Messenger,
``instagram`` is Instagram DM, and the comment surfaces arrive on those same
two objects under ``changes`` rather than ``messaging``. See docs/CHANNELS.md
for the verified contract behind each.

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
from app.channels.outbound import meta_inbound_adapter
from app.channels.registry import any_meta_channel_enabled, meta_dm_channel
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

    Six refusals, and the order of them is the design:

    1. A bad signature is a 403 and nothing else happens. The body is not
       parsed, because parsing unverified input is the thing the signature
       exists to avoid.
    2. No Meta surface enabled at all ACKs and drops, still without parsing.
       Meta keeps delivering to a subscribed webhook regardless of what this
       application thinks, so the switch has to be enforced here rather than
       assumed -- and a deployment with every Meta channel off should not
       spend CPU decoding bodies it will discard.
    3. A body that is valid JSON but not an object ACKs and drops. There is
       no envelope to read, and reaching for one anyway is how this route
       used to answer 500.
    4. An ``object`` this application does not serve ACKs and drops. Meta adds
       products to an existing subscription, so an unfamiliar one is a normal
       event rather than an error.
    5. A known object whose channel is switched off ACKs and drops. This is
       the per-channel check, and it can only happen after the parse, because
       which switch applies is not knowable until the object has been read.
       That is the one behavioural consequence of this ordering: a malformed
       body now answers 400 whenever any Meta surface is enabled, where
       previously it did so only when Messenger specifically was.
    6. Invalid JSON is the only 400 (checked between 2 and 3).

    Everything except the signature and the malformed body answers 200
    deliberately. A non-200 tells Meta the delivery failed and it will retry
    the same payload for hours.
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

    # Ahead of the parse on purpose -- see refusal 2 in the docstring.
    if not any_meta_channel_enabled(channels):
        logger.info("meta_webhook_ignored", reason="no_meta_channel_enabled")
        return {"status": "ignored"}

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    # Valid JSON is not necessarily an envelope. A signed body can decode to a
    # list, a string or a number, and every one of those reached .get() below
    # as an AttributeError that escaped as a 500 -- the one answer that cannot
    # be right, because Meta retries it for hours and the body will never
    # become usable. Dropped with a 200, like every other unusable shape.
    if not isinstance(payload, dict):
        logger.info(
            "meta_webhook_ignored",
            reason="payload_not_an_object",
            payload_type=type(payload).__name__,
        )
        return {"status": "ignored"}

    object_type = str(payload.get("object") or "")
    channel = meta_dm_channel(object_type)
    if channel is None:
        logger.info(
            "meta_webhook_ignored",
            reason="unsupported_object",
            object_type=object_type,
        )
        return {"status": "ignored"}

    if not channels.switches.get(channel, False):
        logger.info(
            "meta_webhook_ignored",
            reason="channel_disabled",
            channel=channel,
            object_type=object_type,
        )
        return {"status": "ignored"}

    if settings.use_task_queue:
        process_meta_webhook_event.delay(payload)
    else:
        background_tasks.add_task(_process_inline, payload)
    return {"status": "queued"}


async def _process_inline(payload: dict[str, Any]) -> None:
    """In-process fallback used when the task queue is disabled (dev only).

    The adapter is chosen from the delivery's own ``object`` rather than fixed,
    so an Instagram payload is parsed by the Instagram adapter and attributed
    to the Instagram channel. Using one adapter for both surfaces would file
    every Instagram conversation under Messenger, and
    ``conversations.channel`` is what every analytics figure groups by.

    Built per delivery and closed here. Unlike the WhatsApp client it is not a
    cached singleton, because it is only reachable on this path and a
    process-wide client would hold a connection pool open for a channel that
    may never be switched on.

    A None adapter is a normal outcome, not an error: the route's checks and
    this one are separated by a queue hop, and configuration can change in
    between.
    """
    adapter = meta_inbound_adapter(str(payload.get("object") or ""))
    if adapter is None:
        logger.info(
            "inline_processing_skipped",
            object_type=str(payload.get("object") or ""),
        )
        return
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
