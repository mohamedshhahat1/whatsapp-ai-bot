"""Chat orchestration: inbound message -> prompt -> AI reply -> outbound send."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.events import conversation_activity, conversation_handoff, publish
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.integrations.openai import OpenAIClient
from app.integrations.whatsapp import WhatsAppClient
from app.models.conversation import MODE_HUMAN, TAG_SALES_LEAD, Conversation
from app.repositories.ai_log import AILogRepository
from app.services import intent, price_policy
from app.services.conversation_service import ConversationService
from app.services.handoff import HANDOFF_ACK, is_sales_lead, wants_human
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

        Used for the opening welcome when there is nothing to answer, for
        out-of-scope questions, and for unsupported message types.
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
        tag: str | None = None,
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

        ``ack``, ``reason`` and ``tag`` vary because there are several ways in.
        Someone who typed 'employee' wants any person; someone haggling over a
        figure wants the Sales Manager specifically, and telling them a generic
        colleague will reply invites another round of it.

        ``tag`` also rides the published event, so a dashboard can raise a
        louder alert for a lead without fetching the row first.
        """
        await self._conversations.conversations.set_mode(
            conversation, MODE_HUMAN, operator=None, tag=tag
        )
        await self._whatsapp.send_text(wa_id, ack)
        await self._conversations.save_outbound(conversation.id, ack)
        await self._session.commit()
        logger.info(
            "handoff_requested_by_customer",
            conversation_id=conversation.id,
            reason=reason,
            tag=tag,
        )
        await publish(
            conversation_handoff(
                conversation_id=conversation.id,
                mode=MODE_HUMAN,
                assigned_operator=None,
                reason=reason,
                tag=conversation.tag,
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

        # Negotiation is checked before wants_human because it is the more
        # specific reading of a message that could match both, and it needs
        # the sales acknowledgement rather than the generic one.
        #
        # There is nothing useful a bot can do once a number is on the table.
        # Agreeing is a commitment it cannot make, refusing is a negotiation it
        # cannot conduct, and deflecting again reads as stonewalling to someone
        # who has already been told once. So this escalates on the first such
        # message rather than counting them -- a customer who says "ok, do it
        # for 1500" has made an offer, and making them repeat it twice more to
        # earn a human is how a live lead goes cold.
        if price_policy.is_negotiating(text):
            logger.info(
                "negotiation_handoff",
                conversation_id=conversation.id,
            )
            await self._begin_handoff(
                wa_id,
                conversation,
                ack=price_policy.sales_handoff_ack(self._settings.sales_phone),
                reason=price_policy.SALES_HANDOFF_REASON,
                tag=TAG_SALES_LEAD,
            )
            return True

        if wants_human(text):
            # Asking for the Sales Manager, or to be called back, is a lead.
            # Asking for 'an employee' with no other signal is not, and
            # tagging it anyway would fill the lead queue with everything and
            # make the top of the operator list meaningless.
            await self._begin_handoff(
                wa_id,
                conversation,
                tag=TAG_SALES_LEAD if is_sales_lead(text) else None,
            )
            return True

        return False

    async def _generate_and_send(
        self,
        wa_id: str,
        name: str | None,
        conversation_id: int,
        history: list[dict],
        retrieval_query: str | None,
        welcome: bool = False,
        general_question: bool = False,
    ) -> None:
        """Build layered instructions, generate a reply, send and persist it.

        ``welcome`` prepends the approved welcome to whatever the model
        produces, including the fallback reply: a customer whose very first
        message arrives while OpenAI is down should still be greeted properly.

        ``general_question`` means the scope check decided no company document
        was needed, so ``retrieval_query`` is None by design rather than by
        accident. The prompt is told which of the two it is.
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
            general_question=general_question,
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
        # layers in front of it did not hold, which is worth knowing.
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

        # ".", "\u061f" or a lone emoji as an opening message: there is nothing
        # to answer, so the welcome and the clarification line are sent as they
        # are and the model is not called at all.
        if first and is_unintelligible(text):
            await self._send_fixed(
                wa_id, conversation.id, f"{WELCOME}\n\n{NOT_UNDERSTOOD}"
            )
            return

        # Scope check, after every gate that could need a human. Ordering
        # matters: a customer asking for a person, or naming a figure, must
        # never be answered with a lecture about what the bot can discuss.
        scope = intent.classify(text)

        if scope == intent.OUT:
            # No OpenAI call and no embedding call. The reply is fixed copy,
            # which also makes the refusal identical every time -- a model
            # improvising this would eventually argue about French politics
            # for a paragraph before declining.
            logger.info("out_of_scope_message", conversation_id=conversation.id)
            reply = intent.out_of_scope_reply(self._settings.company_name)
            if first:
                reply = f"{WELCOME}\n\n{reply}"
            await self._send_fixed(wa_id, conversation.id, reply)
            return

        history = await self._conversations.build_history(conversation.id)

        # A plain price question is NOT escalated. It is the most common
        # opening message in the business, and handing every one of them to a
        # person would put a human on the other end of nearly every new
        # conversation. The model answers it with the deflection, which asks
        # for the area and unit type the Sales Manager needs anyway.
        #
        # A general trade question skips retrieval: searching company
        # documents for "what is drywall" spends an embedding call to return
        # chunks that will not clear the similarity floor anyway.
        await self._generate_and_send(
            wa_id,
            name,
            conversation.id,
            history,
            retrieval_query=text if scope == intent.COMPANY else None,
            welcome=first,
            general_question=scope == intent.DOMAIN,
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

        The scope check is deliberately NOT applied here. A photo has to be
        acknowledged whatever its caption says, and refusing one because the
        caption looked off-topic would leave a customer staring at an
        unanswered picture of their own wall.
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

        # A photo of a damaged wall often carries the request in its caption --
        # including the negotiation. "Do it for 1500" under a picture of a
        # living room is the same offer it would be on its own.
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
