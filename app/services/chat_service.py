"""Chat orchestration: inbound message -> prompt -> AI reply -> outbound send.

The shape of this module is dictated by one fact: two of the four systems it
talks to cannot be rolled back. OpenAI charges for a completion whether or not
we keep it, and WhatsApp's send endpoint has no idempotency key, so once a
request leaves the process we can never afterwards learn whether the customer
saw it.

So the work is split into four short transactions with the slow, irreversible
parts strictly between them:

    1. CLAIM    (txn)  atomically take ownership of the inbound message
    2. READ     (txn)  load conversation state, then release the connection
    3. GENERATE        embedding, vector search, completion -- no txn held
    4. RESERVE  (txn)  write a pending outbound row and commit it
    5. SEND            the WhatsApp call
    6. CONFIRM  (txn)  record that it went

The previous design held a single transaction across all of it. That pinned a
Postgres connection for the several seconds a completion takes, hid the
customer's message from the dashboard until the reply had been sent, and made
the inbound message itself non-durable: a worker killed during generation
rolled it back, and the customer's words were gone.

Stage 4 is the load-bearing one. Committing the intention to reply *before*
sending means a crash mid-send leaves evidence in the database, and the
retried delivery finds it and declines. The trade is explicit and deliberate:
a rare unanswered message in exchange for never sending two. A customer who
gets no reply sends "?" and gets one; a customer who gets two different
answers about their quotation stops trusting the business, and every duplicate
is a second OpenAI charge.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import quota
from app.core.events import conversation_activity, conversation_handoff, publish
from app.core.exceptions import ExternalServiceError
from app.core.idempotency import (
    CachedGeneration,
    clear_generation,
    get_cached_generation,
    store_generation,
)
from app.core.logging import get_logger
from app.core.metrics import DUPLICATE_DELIVERIES_TOTAL
from app.integrations.openai import OpenAIClient
from app.integrations.whatsapp import WhatsAppClient
from app.models.conversation import MODE_HUMAN, TAG_SALES_LEAD, Conversation
from app.models.message import STATUS_SENT, STATUS_UNCONFIRMED
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
        self._retriever = retriever or build_retriever(session, settings)

    async def _release(self) -> None:
        """End the current read transaction and hand the connection back."""
        await self._session.rollback()

    async def _announce(self, conversation_id: int) -> None:
        """Tell connected dashboards that a customer turn landed."""
        await publish(
            conversation_activity(conversation_id=conversation_id, inbound=True),
            self._settings,
        )

    async def _is_first_customer_message(self, conversation_id: int) -> bool:
        """True when the message just stored is the customer's first one here."""
        sent = await self._conversations.messages.count_inbound(conversation_id)
        return sent == 1

    async def _send_once(
        self,
        wa_id: str,
        conversation_id: int,
        text: str,
        reply_to: str | None,
    ) -> bool:
        """Send exactly one message in answer to one inbound message."""
        if reply_to is None:
            result = await self._whatsapp.send_text(wa_id, text)
            out_id = (result.get("messages") or [{}])[0].get("id")
            await self._conversations.save_outbound(
                conversation_id, text, wa_message_id=out_id
            )
            await self._session.commit()
            return True

        reserved_id = await self._conversations.reserve_reply(
            conversation_id, reply_to, text
        )
        if reserved_id is None:
            await self._session.rollback()
            DUPLICATE_DELIVERIES_TOTAL.labels(stage="reply_reserved").inc()
            logger.info(
                "reply_already_reserved",
                conversation_id=conversation_id,
                reply_to=reply_to,
            )
            return False
        await self._session.commit()

        try:
            result = await self._whatsapp.send_text(wa_id, text)
        except ExternalServiceError as exc:
            await self._conversations.messages.confirm_reply(
                reserved_id, None, status=STATUS_UNCONFIRMED
            )
            await self._session.commit()
            logger.error(
                "reply_send_unconfirmed",
                conversation_id=conversation_id,
                reply_to=reply_to,
                error=str(exc),
            )
            return False

        out_id = (result.get("messages") or [{}])[0].get("id")
        await self._conversations.messages.confirm_reply(
            reserved_id, out_id, status=STATUS_SENT
        )
        await self._session.commit()
        return True

    async def _send_fixed(
        self,
        wa_id: str,
        conversation_id: int,
        text: str,
        reply_to: str | None = None,
    ) -> None:
        """Send company copy that needs no model call, and persist it."""
        if await self._send_once(wa_id, conversation_id, text, reply_to):
            await self._announce(conversation_id)

    async def _begin_handoff(
        self,
        wa_id: str,
        conversation: Conversation,
        ack: str = HANDOFF_ACK,
        reason: str = "customer_asked_for_a_human",
        tag: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        """Switch a conversation to a human at the customer's request."""
        await self._conversations.conversations.set_mode(
            conversation, MODE_HUMAN, operator=None, tag=tag
        )
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

        await self._send_once(wa_id, conversation.id, ack, reply_to)
        await self._announce(conversation.id)

    async def _handled_by_human(
        self,
        wa_id: str,
        conversation: Conversation,
        text: str | None,
        reply_to: str | None = None,
    ) -> bool:
        """True when this message must not reach the model."""
        if conversation.mode == MODE_HUMAN:
            logger.info(
                "message_left_for_operator",
                conversation_id=conversation.id,
                assigned_operator=conversation.assigned_operator,
            )
            return True

        if price_policy.is_negotiating(text):
            logger.info("negotiation_handoff", conversation_id=conversation.id)
            await self._begin_handoff(
                wa_id,
                conversation,
                ack=price_policy.sales_handoff_ack(self._settings.sales_phone),
                reason=price_policy.SALES_HANDOFF_REASON,
                tag=TAG_SALES_LEAD,
                reply_to=reply_to,
            )
            return True

        if wants_human(text):
            await self._begin_handoff(
                wa_id,
                conversation,
                tag=TAG_SALES_LEAD if is_sales_lead(text) else None,
                reply_to=reply_to,
            )
            return True

        return False

    async def _within_quota(
        self,
        wa_id: str,
        conversation_id: int,
        wa_message_id: str,
        text: str | None,
    ) -> bool:
        """Check per-customer limits and the global spend guard."""
        decision = await quota.check(wa_id, wa_message_id, text, self._settings)
        if decision.allowed:
            return True

        logger.info(
            "message_declined_by_quota",
            conversation_id=conversation_id,
            reason=decision.reason,
            notified=decision.notify,
        )

        if decision.notify:
            await self._send_fixed(
                wa_id,
                conversation_id,
                quota.message_for(decision, self._settings.sales_phone),
                reply_to=wa_message_id,
            )
        return False

    async def _generate_and_send(
        self,
        wa_id: str,
        name: str | None,
        conversation_id: int,
        history: list[dict],
        retrieval_query: str | None,
        reply_to: str | None = None,
        welcome: bool = False,
        general_question: bool = False,
    ) -> None:
        """Build layered instructions, generate a reply, send and persist it."""
        cached: CachedGeneration | None = None
        if reply_to:
            cached = await get_cached_generation(reply_to, self._settings)
            if cached is not None:
                DUPLICATE_DELIVERIES_TOTAL.labels(stage="generation_cache").inc()

        error: str | None = None
        generation: CachedGeneration | None = None

        if cached is not None:
            reply_text = cached.text
            generation = cached
        else:
            documents: list[RetrievedDocument] = []
            retrieval_attempted = bool(retrieval_query)
            if retrieval_query:
                try:
                    documents = await self._retriever.retrieve(
                        retrieval_query, limit=self._settings.rag_top_k
                    )
                except Exception:
                    logger.error("retrieval_failed", exc_info=True)

            await self._release()

            instructions = self._prompts.build_instructions(
                user_name=name,
                documents=documents,
                retrieval_attempted=retrieval_attempted,
                is_first_message=welcome,
                general_question=general_question,
            )

            reply_text = FALLBACK_REPLY
            try:
                result = await self._ai.generate_reply(
                    history, instructions=instructions
                )
                reply_text = result.text or FALLBACK_REPLY
                generation = CachedGeneration(
                    text=reply_text,
                    model=result.model,
                    prompt_tokens=result.prompt_tokens or 0,
                    completion_tokens=result.completion_tokens or 0,
                    total_tokens=result.total_tokens or 0,
                    latency_ms=result.latency_ms or 0,
                )
                if reply_to:
                    await store_generation(reply_to, generation, self._settings)
                await quota.record_usage(
                    prompt_tokens=generation.prompt_tokens,
                    completion_tokens=generation.completion_tokens,
                    model=generation.model,
                    settings=self._settings,
                )
            except ExternalServiceError as exc:
                error = str(exc)

        if price_policy.mentions_amount(reply_text, self._settings.sales_phone):
            logger.warning(
                "price_leak_blocked",
                conversation_id=conversation_id,
                reply_length=len(reply_text),
            )
            reply_text = price_policy.deflection(self._settings.sales_phone)

        if welcome:
            reply_text = f"{WELCOME}\n\n{reply_text}"

        if generation is not None:
            await self._ai_logs.create(
                model=generation.model,
                conversation_id=conversation_id,
                prompt_tokens=generation.prompt_tokens,
                completion_tokens=generation.completion_tokens,
                total_tokens=generation.total_tokens,
                latency_ms=generation.latency_ms,
            )
        elif error is not None:
            await self._ai_logs.create(
                model=self._settings.openai_model,
                conversation_id=conversation_id,
                error=error,
            )

        sent = await self._send_once(wa_id, conversation_id, reply_text, reply_to)

        if sent:
            if reply_to:
                await clear_generation(reply_to, self._settings)
            await self._announce(conversation_id)

    async def handle_text_message(
        self, wa_id: str, name: str | None, wa_message_id: str, text: str
    ) -> None:
        """Persist an inbound text, generate an AI reply, and send it back."""
        _, conversation = await self._conversations.get_context(wa_id, name)
        claimed = await self._conversations.claim_inbound(
            conversation.id, wa_message_id, type="text", content=text
        )
        if claimed is None:
            await self._session.rollback()
            DUPLICATE_DELIVERIES_TOTAL.labels(stage="inbound_claim").inc()
            logger.info("duplicate_webhook_delivery", wa_message_id=wa_message_id)
            return
        await self._session.commit()

        await self._announce(conversation.id)
        await self._whatsapp.mark_as_read(wa_message_id)

        if await self._handled_by_human(
            wa_id, conversation, text, reply_to=wa_message_id
        ):
            return

        if not await self._within_quota(wa_id, conversation.id, wa_message_id, text):
            return

        first = await self._is_first_customer_message(conversation.id)

        if first and is_unintelligible(text):
            await self._send_fixed(
                wa_id,
                conversation.id,
                f"{WELCOME}\n\n{NOT_UNDERSTOOD}",
                reply_to=wa_message_id,
            )
            return

        scope = intent.classify(text)

        if scope == intent.OUT:
            logger.info("out_of_scope_message", conversation_id=conversation.id)
            reply = intent.out_of_scope_reply(self._settings.company_name)
            if first:
                reply = f"{WELCOME}\n\n{reply}"
            await self._send_fixed(
                wa_id, conversation.id, reply, reply_to=wa_message_id
            )
            return

        history = await self._conversations.build_history(conversation.id)

        await self._generate_and_send(
            wa_id,
            name,
            conversation.id,
            history,
            retrieval_query=text if scope == intent.COMPANY else None,
            reply_to=wa_message_id,
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
        """Persist inbound media (image/document) and respond via the model."""
        _, conversation = await self._conversations.get_context(wa_id, name)
        claimed = await self._conversations.claim_inbound(
            conversation.id,
            wa_message_id,
            type=type,
            content=caption or f"[{type} received]",
            media_id=media_id,
        )
        if claimed is None:
            await self._session.rollback()
            DUPLICATE_DELIVERIES_TOTAL.labels(stage="inbound_claim").inc()
            logger.info("duplicate_webhook_delivery", wa_message_id=wa_message_id)
            return
        await self._session.commit()

        await self._announce(conversation.id)
        await self._whatsapp.mark_as_read(wa_message_id)

        if await self._handled_by_human(
            wa_id, conversation, caption, reply_to=wa_message_id
        ):
            return

        if not await self._within_quota(wa_id, conversation.id, wa_message_id, caption):
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
            reply_to=wa_message_id,
            welcome=first,
        )

    async def handle_unsupported_message(
        self, wa_id: str, name: str | None, wa_message_id: str, type: str
    ) -> None:
        """Politely decline message types the bot does not handle yet."""
        _, conversation = await self._conversations.get_context(wa_id, name)
        claimed = await self._conversations.claim_inbound(
            conversation.id, wa_message_id, type=type, content=f"[{type} received]"
        )
        if claimed is None:
            await self._session.rollback()
            DUPLICATE_DELIVERIES_TOTAL.labels(stage="inbound_claim").inc()
            return
        await self._session.commit()

        await self._announce(conversation.id)

        if await self._handled_by_human(
            wa_id, conversation, None, reply_to=wa_message_id
        ):
            return

        first = await self._is_first_customer_message(conversation.id)
        reply = "Sorry, I can't process that type of message yet. Please send text."
        if first:
            reply = f"{WELCOME}\n\n{reply}"
        await self._send_fixed(wa_id, conversation.id, reply, reply_to=wa_message_id)

    async def handle_status_update(self, wa_message_id: str, status: str) -> None:
        """Record delivery/read/failed status updates for outbound messages."""
        await self._conversations.messages.update_status_by_wa_id(wa_message_id, status)
        await self._session.commit()
