"""Chat orchestration: inbound message -> AI reply -> outbound message."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.integrations.openai import OpenAIClient
from app.integrations.whatsapp import WhatsAppClient
from app.repositories.ai_log import AILogRepository
from app.services.conversation_service import ConversationService

logger = get_logger(__name__)

FALLBACK_REPLY = (
    "Sorry, I'm having trouble responding right now. Please try again in a moment."
)


class ChatService:
    """End-to-end handling of WhatsApp events."""

    def __init__(
        self,
        session: AsyncSession,
        whatsapp: WhatsAppClient,
        ai: OpenAIClient,
        settings: Settings,
    ) -> None:
        self._session = session
        self._whatsapp = whatsapp
        self._ai = ai
        self._settings = settings
        self._conversations = ConversationService(session, settings)
        self._ai_logs = AILogRepository(session)

    async def handle_text_message(
        self, wa_id: str, name: str | None, wa_message_id: str, text: str
    ) -> None:
        """Persist an inbound text, generate an AI reply, and send it back."""
        if await self._conversations.messages.exists_by_wa_id(wa_message_id):
            logger.info("duplicate_webhook_delivery", wa_message_id=wa_message_id)
            return

        _, conversation = await self._conversations.get_context(wa_id, name)
        await self._conversations.save_inbound(
            conversation.id, wa_message_id, type="text", content=text
        )
        await self._whatsapp.mark_as_read(wa_message_id)

        history = await self._conversations.build_history(conversation.id)
        reply_text = FALLBACK_REPLY
        try:
            result = await self._ai.generate_reply(history)
            reply_text = result.text or FALLBACK_REPLY
            await self._ai_logs.create(
                model=result.model,
                conversation_id=conversation.id,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                total_tokens=result.total_tokens,
                latency_ms=result.latency_ms,
            )
        except ExternalServiceError as exc:
            await self._ai_logs.create(
                model=self._settings.openai_model,
                conversation_id=conversation.id,
                error=str(exc),
            )

        send_result = await self._whatsapp.send_text(wa_id, reply_text)
        out_id = (send_result.get("messages") or [{}])[0].get("id")
        await self._conversations.save_outbound(
            conversation.id, reply_text, wa_message_id=out_id
        )
        await self._session.commit()

    async def handle_media_message(
        self,
        wa_id: str,
        name: str | None,
        wa_message_id: str,
        type: str,
        media_id: str | None,
        caption: str | None,
    ) -> None:
        """Persist inbound media (image/document) and respond via the model."""
        if await self._conversations.messages.exists_by_wa_id(wa_message_id):
            return

        _, conversation = await self._conversations.get_context(wa_id, name)
        await self._conversations.save_inbound(
            conversation.id,
            wa_message_id,
            type=type,
            content=caption or f"[{type} received]",
            media_id=media_id,
        )
        await self._whatsapp.mark_as_read(wa_message_id)

        history = await self._conversations.build_history(conversation.id)
        history.append(
            {
                "role": "user",
                "content": (
                    f"(The user sent a {type}"
                    + (f' with caption: "{caption}")' if caption else ")")
                    + " Acknowledge it briefly and ask how you can help."
                ),
            }
        )
        reply_text = FALLBACK_REPLY
        try:
            result = await self._ai.generate_reply(history)
            reply_text = result.text or FALLBACK_REPLY
            await self._ai_logs.create(
                model=result.model,
                conversation_id=conversation.id,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                total_tokens=result.total_tokens,
                latency_ms=result.latency_ms,
            )
        except ExternalServiceError as exc:
            await self._ai_logs.create(
                model=self._settings.openai_model,
                conversation_id=conversation.id,
                error=str(exc),
            )

        send_result = await self._whatsapp.send_text(wa_id, reply_text)
        out_id = (send_result.get("messages") or [{}])[0].get("id")
        await self._conversations.save_outbound(
            conversation.id, reply_text, wa_message_id=out_id
        )
        await self._session.commit()

    async def handle_unsupported_message(
        self, wa_id: str, name: str | None, wa_message_id: str, type: str
    ) -> None:
        """Politely decline message types the bot does not handle yet."""
        if await self._conversations.messages.exists_by_wa_id(wa_message_id):
            return
        _, conversation = await self._conversations.get_context(wa_id, name)
        await self._conversations.save_inbound(
            conversation.id, wa_message_id, type=type, content=f"[{type} received]"
        )
        reply = "Sorry, I can't process that type of message yet. Please send text."
        await self._whatsapp.send_text(wa_id, reply)
        await self._conversations.save_outbound(conversation.id, reply)
        await self._session.commit()

    async def handle_status_update(self, wa_message_id: str, status: str) -> None:
        """Record delivery/read/failed status updates for outbound messages."""
        await self._conversations.messages.update_status_by_wa_id(
            wa_message_id, status
        )
        await self._session.commit()
