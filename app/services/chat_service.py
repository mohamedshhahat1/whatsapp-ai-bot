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
        # Defaulting here (rather than in every caller) means the API and the
        # Celery worker both get RAG without duplicating the wiring, while the
        # parameter keeps the retriever injectable for tests.
        self._retriever = retriever or build_retriever(session, settings)

    # -----------------------------------------------------------------
    # Transaction helpers
    # -----------------------------------------------------------------

    async def _release(self) -> None:
        """End the current read transaction and hand the connection back.

        SQLAlchemy opens a transaction on the first statement and holds the
        connection until it is committed or rolled back -- including for pure
        reads. Without this, the snapshot taken while loading history would
        stay open across the OpenAI call, which is the exact problem this
        module was restructured to remove.

        Rollback rather than commit because nothing was written. It is also
        what clears the failed-transaction state after an error, so the next
        stage can still run its own work.
        """
        await self._session.rollback()

    # -----------------------------------------------------------------
    # Notifications
    # -----------------------------------------------------------------

    async def _announce(self, conversation_id: int) -> None:
        """Tell connected dashboards that a customer turn landed.

        Called only after the relevant transaction has committed. The dashboard
        reacts by refetching through the admin API, so announcing uncommitted
        work would point the operator at rows that do not exist yet.

        Now fired immediately after the inbound message is committed rather
        than after the reply is sent. An operator watching a customer type
        should see it arrive, not learn about it several seconds later once the
        bot has already answered -- and if generation fails entirely, the
        message must still show up.
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

    # -----------------------------------------------------------------
    # Sending
    # -----------------------------------------------------------------

    async def _send_once(
        self,
        wa_id: str,
        conversation_id: int,
        text: str,
        reply_to: str | None,
    ) -> bool:
        """Send exactly one message in answer to one inbound message.

        Returns True if this call sent it, False if someone else already had.

        This is the reserve -> send -> confirm sequence, and every outbound
        path goes through it so that fixed copy, handoff acknowledgements and
        model replies are all equally safe to retry. A welcome sent twice is a
        smaller problem than a duplicated quotation, but it is still a bug, and
        having one path means there is one place to reason about.

        ``reply_to`` is None only for sends that are not answers to an inbound
        message. Those cannot be deduplicated -- there is no key to deduplicate
        against -- so they fall back to send-then-record.
        """
        if reply_to is None:
            result = await self._whatsapp.send_text(wa_id, text)
            out_id = (result.get("messages") or [{}])[0].get("id")
            await self._conversations.save_outbound(
                conversation_id, text, wa_message_id=out_id
            )
            await self._session.commit()
            return True

        # --- Stage: RESERVE ------------------------------------------------
        # Committed before the send, on purpose. This row is the only durable
        # record that a send may have happened.
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

        # --- Stage: SEND ---------------------------------------------------
        try:
            result = await self._whatsapp.send_text(wa_id, text)
        except ExternalServiceError as exc:
            # The reservation is deliberately NOT released.
            #
            # WhatsAppClient already retries transient failures with backoff,
            # so by the time this escapes the request has been attempted
            # several times and we genuinely cannot tell whether one of them
            # was delivered. Freeing the reservation would let the Celery retry
            # send again, which is how a customer receives the same answer
            # twice. Leaving it marks the reply unconfirmed and stops there.
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

        # --- Stage: CONFIRM ------------------------------------------------
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
        """Send company copy that needs no model call, and persist it.

        Used for the opening welcome when there is nothing to answer, for
        out-of-scope questions, for quota refusals and for unsupported message
        types.
        """
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

        The mode change is committed before the acknowledgement is sent. If the
        send fails, the conversation is still handed over -- the operator sees
        it and can pick it up. The reverse order would leave the bot answering
        a customer who has been told a human is coming.
        """
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
        """True when this message must not reach the model.

        Called after the inbound message has been committed and announced, and
        before any generation: a message is always stored and always shown to
        operators. Only the AI reply is skipped.
        """
        if conversation.mode == MODE_HUMAN:
            logger.info(
                "message_left_for_operator",
                conversation_id=conversation.id,
                assigned_operator=conversation.assigned_operator,
            )
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
            # Asking for the Sales Manager, or to be called back, is a lead.
            # Asking for 'an employee' with no other signal is not, and
            # tagging it anyway would fill the lead queue with everything and
            # make the top of the operator list meaningless.
            await self._begin_handoff(
                wa_id,
                conversation,
                tag=TAG_SALES_LEAD if is_sales_lead(text) else None,
                reply_to=reply_to,
            )
            return True

        return False

    # -----------------------------------------------------------------
    # Quota
    # -----------------------------------------------------------------

    async def _within_quota(
        self,
        wa_id: str,
        conversation_id: int,
        wa_message_id: str,
        text: str | None,
    ) -> bool:
        """Check per-customer limits and the global spend guard.

        Runs after the message is stored and before anything is paid for. The
        ordering is the point: a throttled customer's messages still appear in
        the operator's transcript, because we decline to *answer*, never to
        *listen*.

        A refusal is sent at most once per episode -- see ``QuotaDecision``.
        Replying to every message of a flood would make the refusals the flood.
        """
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

    # -----------------------------------------------------------------
    # Generation
    # -----------------------------------------------------------------

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
        """Build layered instructions, generate a reply, send and persist it.

        ``welcome`` prepends the approved welcome to whatever the model
        produces, including the fallback reply: a customer whose very first
        message arrives while OpenAI is down should still be greeted properly.

        ``general_question`` means the scope check decided no company document
        was needed, so ``retrieval_query`` is None by design rather than by
        accident. The prompt is told which of the two it is.

        No transaction is open while the model is called. The AI log is written
        in the same transaction as the reservation, so one commit records both
        what we spent and what we are about to say.
        """
        cached: CachedGeneration | None = None
        if reply_to:
            cached = await get_cached_generation(reply_to, self._settings)
            if cached is not None:
                DUPLICATE_DELIVERIES_TOTAL.labels(stage="generation_cache").inc()

        error: str | None = None
        # Declared up front rather than inferred from the first branch. mypy
        # types an unannotated local from its first assignment, which here is
        # `generation = cached` (already narrowed to CachedGeneration), and
        # would then reject the `None` in the else branch.
        generation: CachedGeneration | None = None

        if cached is not None:
            # A previous attempt already paid for this answer. Reuse it rather
            # than buying a second one that says almost the same thing.
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
                    # Retrieval must never break the conversation. The prompt is
                    # still told a search happened and returned nothing, so the
                    # model declines to answer from memory rather than inventing.
                    logger.error("retrieval_failed", exc_info=True)

            # Retrieval is the last database work before the completion: the
            # embedding call is external and the vector search is a read. Let
            # the connection go before the slowest call in the request.
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
                # Usage fields are optional in the API response; log 0 when absent.
                generation = CachedGeneration(
                    text=reply_text,
                    model=result.model,
                    prompt_tokens=result.prompt_tokens or 0,
                    completion_tokens=result.completion_tokens or 0,
                    total_tokens=result.total_tokens or 0,
                    latency_ms=result.latency_ms,
                )
                # Cached before any database write, because the window a crash
                # opens is precisely the one where the write never happens and
                # the completion has already been billed.
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

        # --- Stage: RESERVE (+ the AI log, in the same transaction) ---------
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
            # The reply is durably recorded, so the cached completion has done
            # its job. Dropping it keeps Redis proportional to in-flight work.
            if reply_to:
                await clear_generation(reply_to, self._settings)
            await self._announce(conversation_id)

    # -----------------------------------------------------------------
    # Entry points
    # -----------------------------------------------------------------

    async def handle_text_message(
        self, wa_id: str, name: str | None, wa_message_id: str, text: str
    ) -> None:
        """Persist an inbound text, generate an AI reply, and send it back."""
        # --- Stage: CLAIM ---------------------------------------------------
        # One statement decides whether this delivery is ours to process. The
        # old exists-then-insert let two concurrent redeliveries both pass.
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

        # The customer's message is now durable. Everything after this point
        # can crash without losing it, and the operator can already see it.
        await self._announce(conversation.id)
        await self._whatsapp.mark_as_read(wa_message_id)

        if await self._handled_by_human(
            wa_id, conversation, text, reply_to=wa_message_id
        ):
            return

        if not await self._within_quota(wa_id, conversation.id, wa_message_id, text):
            return

        # --- Stage: READ ----------------------------------------------------
        first = await self._is_first_customer_message(conversation.id)

        # ".", "\u061f" or a lone emoji as an opening message: there is nothing
        # to answer, so the welcome and the clarification line are sent as they
        # are and the model is not called at all.
        if first and is_unintelligible(text):
            await self._send_fixed(
                wa_id,
                conversation.id,
                f"{WELCOME}\n\n{NOT_UNDERSTOOD}",
                reply_to=wa_message_id,
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
            await self._send_fixed(
                wa_id, conversation.id, reply, reply_to=wa_message_id
            )
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
        """Persist inbound media (image/document) and respond via the model.

        The file itself is never sent to the model: only the fact that one
        arrived, plus its caption. The persona is explicit that it cannot see
        images, so it acknowledges and asks instead of describing.

        The scope check is deliberately NOT applied here. A photo has to be
        acknowledged whatever its caption says, and refusing one because the
        caption looked off-topic would leave a customer staring at an
        unanswered picture of their own wall.
        """
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

        # A photo of a damaged wall often carries the request in its caption --
        # including the negotiation. "Do it for 1500" under a picture of a
        # living room is the same offer it would be on its own.
        if await self._handled_by_human(
            wa_id, conversation, caption, reply_to=wa_message_id
        ):
            return

        # Media costs the same completion as text, so it goes through the same
        # quota gate. Sending twenty photos in a row is one of the cheaper ways
        # to run up a bill.
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

        # While a human owns the conversation the bot says nothing at all --
        # not even this. The operator can see the voice note in the transcript.
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
        """Record delivery/read/failed status updates for outbound messages.

        Deliberately not announced to the dashboard. Every outbound message
        produces sent/delivered/read callbacks, which would triple the event
        volume to move a label the operator is not waiting on. The next real
        activity refetch picks the status up.

        This is also how an ``unconfirmed`` reply resolves itself: if the send
        did reach Meta, a status callback arrives carrying its id and the row
        is updated to delivered.
        """
        await self._conversations.messages.update_status_by_wa_id(wa_message_id, status)
        await self._session.commit()
