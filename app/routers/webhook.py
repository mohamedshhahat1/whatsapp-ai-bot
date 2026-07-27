"""WhatsApp Cloud API webhook: verification and event ingestion."""

import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.config import get_settings
from app.core.logging import get_logger
from app.core.security import verify_meta_signature
from app.db.session import SessionLocal
from app.dependencies.deps import get_openai_client, get_whatsapp_client
from app.services.chat_service import ChatService

logger = get_logger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])


@router.get("")
async def verify_webhook(request: Request) -> PlainTextResponse:
    """Meta webhook verification handshake (hub.challenge echo)."""
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == get_settings().whatsapp_verify_token
    ):
        return PlainTextResponse(params.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("")
async def receive_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> dict[str, str]:
    """Validate the Meta signature, ACK fast, process in the background."""
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_meta_signature(
        get_settings().whatsapp_app_secret, raw_body, signature
    ):
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    background_tasks.add_task(process_webhook_payload, payload)
    return {"status": "received"}


async def process_webhook_payload(payload: dict[str, Any]) -> None:
    """Process messages and status updates from a webhook delivery."""
    async with SessionLocal() as session:
        service = ChatService(
            session, get_whatsapp_client(), get_openai_client(), get_settings()
        )
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                contacts = {
                    contact.get("wa_id"): contact.get("profile", {}).get("name")
                    for contact in value.get("contacts", [])
                }
                for message in value.get("messages", []):
                    await _dispatch_message(service, message, contacts)
                for status in value.get("statuses", []):
                    wa_message_id = status.get("id")
                    new_status = status.get("status")
                    if wa_message_id and new_status:
                        await service.handle_status_update(wa_message_id, new_status)


async def _dispatch_message(
    service: ChatService, message: dict[str, Any], contacts: dict[str, str | None]
) -> None:
    """Route a single inbound message to the right handler."""
    wa_id = message.get("from", "")
    wa_message_id = message.get("id", "")
    message_type = message.get("type", "unknown")
    name = contacts.get(wa_id)
    try:
        if message_type == "text":
            await service.handle_text_message(
                wa_id, name, wa_message_id, message.get("text", {}).get("body", "")
            )
        elif message_type in ("image", "document"):
            media = message.get(message_type, {})
            await service.handle_media_message(
                wa_id,
                name,
                wa_message_id,
                message_type,
                media.get("id"),
                media.get("caption"),
            )
        else:
            await service.handle_unsupported_message(
                wa_id, name, wa_message_id, message_type
            )
    except Exception:
        # Never crash webhook processing; Meta retries deliveries.
        logger.error(
            "message_processing_failed",
            wa_message_id=wa_message_id,
            type=message_type,
            exc_info=True,
        )
