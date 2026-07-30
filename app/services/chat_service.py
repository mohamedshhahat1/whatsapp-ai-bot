"""Chat orchestration: inbound message -> prompt -> AI reply -> outbound send."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.events import conversation_activity, conversation_handoff, publish
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.integrations.openai import OpenAIClient
from app.integrations.whatsapp import WhatsAppClient
from app.models.conversation import MODE_HUMAN, Conversation
from app.repositories.ai_log import AILogRepository
from app.services import price_policy
from app.services.conversation_service import ConversationService
from app.services.handoff import HANDOFF_ACK, wants_human
from app.services.persona import NOT_UNDERSTOOD, WELCOME, is_unintelligible
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

    async def _is_first_customer_message(self, conversation_id: int) -> bool:
        """True when the message just stored is the customer's first one here.

        Counted in the database rather than inferred by the model. The welcome
        is approved copy that must appear exactly once, and a model holding
        twenty turns of history will eventually send it twice or not at all.
        """
        sent = await self._conversations.messages.count_inbound(conversation_id)
        return sent == 1

    async def _send_fixed(self, wa_id: str, conversation_id: int, text: str) -> None:
        """Send company copy that needs no model call, and persist it.

        Used for the opening welcome when there is nothing to answer, and for
        unsupported message types.
        """
        result = await self._whatsapp.send_text(wa_id, text)
        out_id = (result.get("messages") or [{}])[0].get("id")
        await self._conversations.save_outbound(
            conversation_id, text, wa_message_id=out_id
        )
        await self._session.commit()
        await self._announce(conversation_id)

    async def _begin_handoff(
        self,
        wa_id: str,
        conversation: Conversation,
        ack: str = HANDOFF_ACK,
        reason: str = "customer_asked_for_a_human",
    ) -> None:
        """Switch a conversation to a human at the customer's request.

        No operator is assigned: nobody has claimed it yet. The dashboard shows
        it as unassigned, and whoever presses Take Over becomes the owner.

        One acknowledgement is sent. Going silent immediately would be the
        worst outcome for someone who just asked for a person, and it is the
        last thing the bot says on this conversation until the AI is resumed.
        The welcome is deliberately NOT prepended here, even on a first
        message: a service menu inviting questions would contradict a message
        that says a colleague is taking over.

        ``ack`` and ``reason`` vary because there are two ways in. Someone who
        typed 'employee' wants any person; someone who has asked the price
        three times wants the Sales Manager specifically, and telling them a
        generic colleague will reply invites a fourth ask.
        """
        await self._conversations.conversations.set_mode(
            conversation, MODE_HUMAN, operator=None
        )
        await self._whatsapp.send_text(wa_id, ack)
        await self._conversations.save_outbound(conversation.id, ack)
        await self._session.commit()
        logger.info(
            "handoff_requested_by_customer",
            conversation_id=conversation.id,
            reason=reason,
        )
        await publish(
            conversation_handoff(
                conversation_id=conversation.id,
                mode=MODE_HUMAN,
                assigned_operator=None,
                reason=reason,
            ),
            self._settings,
        )
        await self._announce(conversation.id)

    async def _handled_by_human(
        self, wa_id: str, conversation: Conversation, text: str | None
    ) -> bool:
        """True when this message must not reach the model.

        Called after the inbound message has been persisted and marked read,
        and before any generation: a message is always stored and always
        announced to the dashboard. Only the AI reply is skipped.
        """
        if conversation.mode == MODE_HUMAN:
            await self._session.commit()
            logger.info(
                "message_left_for_operator",
                conversation_id=conversation.id,
                assigned_operator=conversation.assigned_operator,
            )
            await self._announce(conversation.id)
            return True

        if wants_human(text):
            await self._begin_handoff(wa_id, conversation)
            return True

        return False

    def _price_pressure(self, text: str | None, history: list[dict]) -> bool:
        """True when the customer has raised money too many times to continue.

        The current message must itself be about money -- otherwise a customer
        who asked twice, got a good answer about materials, and then asked a
        third unrelated question would be silently handed to sales.

        The count is taken over the history window the model sees, not the
        whole conversation. Someone who asked about price last month and twice
        today is not applying pressure, and the window makes the threshold
        mean "in this conversation" rather than "ever".
        """
        if not price_policy.asks_about_price(text):
            return False
        return price_policy.count_price_asks(history) >= price_policy.INSIST_THRESHOLD

    async def _generate_and_send(
        self,
        wa_id: str,
        name: str | None,
        conversation_id: int,
        history: list[dict],
        retrieval_query: str | None,
        welcome: bool = False,
    ) -> None:
        """Build layered instructions, generate a reply, send and persist it.

        ``welcome`` prepends the approved welcome to whatever the model
        produces, including the fallback reply: a customer whose very first
        message arrives while OpenAI is down should still be greeted properly.
        """
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
                # model declines to answer from memory rather than inventing.
                logger.error("retrieval_failed", exc_info=True)

        instructions = self._prompts.build_instructions(
            user_name=name,
            documents=documents,
            retrieval_attempted=retrieval_attempted,
            is_first_message=welcome,
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

        # The last gate before a customer sees anything. A reply carrying a
        # figure is discarded whole rather than edited: a sentence with its
        # number stripped out reads as evasive and often leaves the amount
        # implied by what surrounds it. Approved copy is the only version that
        # cannot leak. Logged at warning level because a hit here means the
        # three layers in front of it did not hold, which is worth knowing.
        if price_policy.mentions_amount(reply_text, self._settings.sales_phone):
            logger.warning(
                "price_leak_blocked",
                conversation_id=conversation_id,
                reply_length=len(reply_text),
            )
            reply_text = price_policy.deflection(self._settings.sales_phone)

        if welcome:
            reply_text = f"{WELCOME}\n\n{reply_text}"

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

        if await self._handled_by_human(wa_id, conversation, text):
            return

        first = await self._is_first_customer_message(conversation.id)

        # ".", "؟" or a lone emoji as an opening message: there is nothing to
        # answer, so the welcome and the clarification line are sent as they
        # are and the model is not called at all.
        if first and is_unintelligible(text):
            await self._send_fixed(
                wa_id, conversation.id, f"{WELCOME}\n\n{NOT_UNDERSTOOD}"
            )
            return

        history = await self._conversations.build_history(conversation.id)

        # A customer who will not accept "a colleague will quote you" is not
        # going to accept it on the fourth attempt either, and every further
        # deflection reads as stonewalling. Hand them to sales instead.
        if self._price_pressure(text, history):
            logger.info(
                "price_pressure_handoff",
                conversation_id=conversation.id,
                asks=price_policy.count_price_asks(history),
            )
            await self._begin_handoff(
                wa_id,
                conversation,
                ack=price_policy.sales_handoff_ack(self._settings.sales_phone),
                reason=price_policy.SALES_HANDOFF_REASON,
            )
            return

        await self._generate_and_send(
            wa_id,
            name,
            conversation.id,
            history,
            retrieval_query=text,
            welcome=first,
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
        """Persist inbound media (image/document) and respond via the model.

        The file itself is never sent to the model: only the fact that one
        arrived, plus its caption. The persona is explicit that it cannot see
        images, so it acknowledges and asks instead of describing.
        """
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

        # A photo of a damaged wall often carries the request in its caption.
        if await self._handled_by_human(wa_id, conversation, caption):
            return

        first = await self._is_first_customer_message(conversation.id)

        history = await self._conversations.build_history(conversation.id)
        history.append(
            {
                "role": "user",
                "content": (
                    f"(The user sent a {type}"
                    + (f' with caption: "{caption}")' if caption else ")")
                    + " You cannot see its contents. Confirm that it arrived,"
                    + " do not describe or guess what is in it, and ask what"
                    + " they would like done."
                ),
            }
        )
        await self._generate_and_send(
            wa_id,
            name,
            conversation.id,
            history,
            retrieval_query=caption,
            welcome=first,
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

        # While a human owns the conversation the bot says nothing at all --
        # not even this. The operator can see the voice note in the transcript.
        if await self._handled_by_human(wa_id, conversation, None):
            return

        first = await self._is_first_customer_message(conversation.id)
        reply = "Sorry, I can't process that type of message yet. Please send text."
        if first:
            reply = f"{WELCOME}\n\n{reply}"
        await self._send_fixed(wa_id, conversation.id, reply)

    async def handle_status_update(self, wa_message_id: str, status: str) -> None:
        """Record delivery/read/failed status updates for outbound messages.

        Deliberately not announced to the dashboard. Every outbound message
        produces sent/delivered/read callbacks, which would triple the event
        volume to move a label the operator is not waiting on. The next real
        activity refetch picks the status up.
        """
        await self._conversations.messages.update_status_by_wa_id(wa_message_id, status)
        await self._session.commit()
