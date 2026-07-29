"""Chat orchestration: inbound message -> prompt -> AI reply -> outbound send."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.events import conversation_activity, publish
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.integrations.openai import OpenAIClient
from app.integrations.whatsapp import WhatsAppClient
from app.repositories.ai_log import AILogRepository
from app.services.conversation_service import ConversationService
from app.services.prompt_builder import PromptBuilder
from app.services.retrieval import (
    DocumentRetriever,
    RetrievedDocument,
    build_retriever,
)

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
        retriever: DocumentRetriever | None = None,
    ) -> None:
        self._session = session
        self._whatsapp = whatsapp
        self._ai = ai
        self._settings = settings
        self._conversations = ConversationService(session, settings)
        self._ai_logs = AILogRepository(session)
        self._prompts = PromptBuilder(settings)
        # Defaulting here (rather than in every caller) means the API and the
        # Celery worker both get RAG without duplicating the wiring, while the
        # parameter keeps the retriever injectable for tests.
        self._retriever = retriever or build_retriever(session, settings)

    async def _announce(self, conversation_id: int) -> None:
        """Tell connected dashboards that a customer turn landed.

        Called only after the transaction has committed. The dashboard reacts
        by refetching through the admin API, so announcing uncommitted work
        would point the operator at rows that do not exist yet -- and if the
        transaction rolled back and Celery retried the delivery, the
        notification would already have been sent for a message that never
        existed.
        """
        await publish(
            conversation_activity(conversation_id=conversation_id, inbound=True),
            self._settings,
        )

    async def _generate_and_send(
        self,
        wa_id: str,
        name: str | None,
        conversation_id: int,
        history: list[dict],
        retrieval_query: str | None,
    ) -> None:
        """Build layered instructions, generate a reply, send and persist it."""
        documents: list[RetrievedDocument] = []
        retrieval_attempted = bool(retrieval_query)
        if retrieval_query:
            try:
                documents = await self._retriever.retrieve(
                    retrieval_query, limit=self._settings.rag_top_k
                )
            except Exception:
                # Retrieval must never break the conversation. The prompt is
                # still told a search happened and returned nothing, so the
                # model declines to quote prices rather than inventing them.
                logger.error("retrieval_failed", exc_info=True)

        instructions = self._prompts.build_instructions(
            user_name=name,
            documents=documents,
            retrieval_attempted=retrieval_attempted,
        )

        reply_text = FALLBACK_REPLY
        try:
            result = await self._ai.generate_reply(history, instructions=instructions)
            reply_text = result.text or FALLBACK_REPLY
            # Usage fields are optional in the API response; log 0 when absent.
            await self._ai_logs.create(
                model=result.model,
                conversation_id=conversation_id,
                prompt_tokens=result.prompt_tokens or 0,
                completion_tokens=result.completion_tokens or 0,
                total_tokens=result.total_tokens or 0,
                latency_ms=result.latency_ms,
            )
        except ExternalServiceError as exc:
            await self._ai_logs.create(
                model=self._settings.openai_model,
                conversation_id=conversation_id,
                error=str(exc),
            )

        send_result = await self._whatsapp.send_text(wa_id, reply_text)
        out_id = (send_result.get("messages") or [{}])[0].get("id")
        await self._conversations.save_outbound(
            conversation_id, reply_text, wa_message_id=out_id
        )
        await self._session.commit()
        await self._announce(conversation_id)

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
        await self._generate_and_send(
            wa_id, name, conversation.id, history, retrieval_query=text
        )

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
        await self._generate_and_send(
            wa_id, name, conversation.id, history, retrieval_query=caption
        )

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
        await self._announce(conversation.id)

    async def handle_status_update(self, wa_message_id: str, status: str) -> None:
        """Record delivery/read/failed status updates for outbound messages.

        Deliberately not announced to the dashboard. Every outbound message
        produces sent/delivered/read callbacks, which would triple the event
        volume to move a label the operator is not waiting on. The next real
        activity refetch picks the status up.
        """
        await self._conversations.messages.update_status_by_wa_id(wa_message_id, status)
        await self._session.commit()
