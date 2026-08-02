"""Conversation session lifecycle: welcome, idle timer, closing, reopen.

A session is one complete visit -- hello, some questions, goodbye -- and this
module owns the three moments that bound it.

Why a sweeper and not a scheduled job per session
-------------------------------------------------
The obvious implementation is to schedule a "close this conversation" task
five minutes ahead on every message and cancel it when the next one arrives.
It is also the wrong one here, for reasons that only show up in production:

* Every message would schedule a job, so a customer sending eight messages
  leaves eight jobs, seven of which must be revoked. Celery revocation is
  best-effort and does not survive a broker restart, so the survivors fire
  against a conversation that is no longer idle.
* The pending jobs live in Redis. A broker flush, a failover, or a Redis
  restart without persistence loses them all, and those sessions then stay
  open forever with nothing left to close them.
* Correctness would depend on cancellation winning a race with execution.

Sweeping instead inverts the dependency: the schedule holds no state, and the
database -- the one component here that is already durable and already the
source of truth -- answers "what is idle?" on demand. A restarted worker, a
flushed broker or a second replica changes nothing, because there was never
anything to lose. Beat re-emitting a tick early is harmless for the same
reason a redelivered tick is: the claim below is idempotent.

Exactly one goodbye
-------------------
``claim_idle_sessions`` takes ``closing_sent_at`` with a conditional UPDATE
and that claim is COMMITTED BEFORE the WhatsApp call, exactly as
``MessageRepository.reserve_reply`` commits before sending a reply. WhatsApp's
send endpoint has no idempotency key, so a crash mid-send can never afterwards
be distinguished from a success. Writing the intention first means the retry
finds the claim and declines, trading a rare missing goodbye for a goodbye
that is never sent twice. For a closing message that trade is even easier than
it is for a reply: nobody minds an absent pleasantry, and everybody notices
being thanked for their enquiry twice.

Configuration
-------------
Every duration and every behaviour here comes from ``Settings``; there are no
timeouts in this module. ``enable_conversation_session`` is the master switch
and is checked at each of the three entry points below rather than at the
call sites, so turning the feature off cannot be defeated by a caller that
forgot to ask. See ``docs/SESSION_LIFECYCLE.md``.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.events import conversation_activity, publish
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.integrations.whatsapp import WhatsAppClient
from app.models.conversation import Conversation
from app.repositories.conversation import ConversationRepository
from app.repositories.message import MessageRepository
from app.services.persona import CLOSING
from app.services.reply_service import CUSTOMER_SERVICE_WINDOW

logger = get_logger(__name__)

# Most one pass will close. Bounds the transaction and the number of WhatsApp
# calls made back to back after an outage; whatever is left waits for the next
# tick, a minute later.
SWEEP_BATCH_SIZE = 200


class SessionService:
    """Owns when a session starts, when it is idle, and when it ends."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        whatsapp: WhatsAppClient | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._whatsapp = whatsapp
        self._conversations = ConversationRepository(session)
        self._messages = MessageRepository(session)

    @property
    def enabled(self) -> bool:
        """Whether the lifecycle runs at all (ENABLE_CONVERSATION_SESSION).

        Off, conversations behave exactly as they did before this feature
        existed: one endless thread per customer, greeted once when it is
        created and never closed. That is the point -- it is a way to switch
        the whole thing off in an incident without a rollback.
        """
        return self._settings.enable_conversation_session

    # --- Welcome ------------------------------------------------------------

    async def should_welcome(self, conversation: Conversation) -> bool:
        """Whether this session still owes its customer a greeting.

        Gated on ``welcome_sent_at`` rather than on the inbound message count.
        The count answers a subtly different question -- "is this their first
        message?" -- and the two diverge in the case that matters: if the reply
        carrying the welcome fails to send, the count is still one, the next
        message makes it two, and that customer is never greeted at all.

        A session resumed inside the reopen window arrives here with
        ``welcome_sent_at`` still set, which is exactly why resuming rather
        than copying was the right shape: the "do not greet them twice" rule
        needs no special case for it.
        """
        if not self.enabled:
            return False
        if not self._settings.enable_welcome_on_new_session:
            return False
        if (
            self._settings.prevent_duplicate_welcome
            and conversation.welcome_sent_at is not None
        ):
            return False
        if self._settings.enable_repeat_welcome_after_new_session:
            return True
        # Repeat welcomes are off: greet only a customer who has never had a
        # conversation before this one.
        return await self._conversations.count_for_user(conversation.user_id) <= 1

    async def mark_welcome_sent(self, conversation_id: int) -> None:
        """Record a delivered welcome. Does not commit."""
        await self._conversations.mark_welcome_sent(conversation_id)

    # --- Idle timer ---------------------------------------------------------

    async def touch(self, conversation_id: int, *, outgoing: bool = False) -> None:
        """Reset the idle timer. Does not commit.

        Deliberately called for activity in BOTH directions. Counting only
        customer messages would start the clock the moment they stopped
        typing, so a long answer -- a retrieval, a completion and two API
        calls -- could be overtaken by the sweeper and followed by a goodbye.

        ``outgoing`` marks a reply rather than a customer message, so that
        RESET_IDLE_TIMER_ON_OUTGOING_MESSAGE can switch that behaviour off and
        make the timer measure silence from the customer alone. Leaving it off
        is not recommended and the flag exists mainly to make the choice
        visible: with it off, the race described above is live again.
        """
        if not self.enabled:
            return
        if outgoing and not self._settings.reset_idle_timer_on_outgoing_message:
            return
        await self._conversations.touch(conversation_id)

    # --- Closing ------------------------------------------------------------

    @property
    def closing_text(self) -> str:
        """The configured closing copy, or the approved persona default.

        Mirrors how ``system_prompt`` defers to the persona: the default is
        multi-line text a customer reads, which belongs in version control
        where it can be reviewed, not in a ``.env`` entry that cannot hold a
        newline without escaping games.
        """
        return self._settings.conversation_closing_message.strip() or CLOSING

    async def close_idle_sessions(self) -> int:
        """Close every session that has gone idle. Returns how many.

        Ordering is the point of this method:

            1. claim   -- conditional UPDATE, then COMMIT
            2. send    -- the WhatsApp call, outside any transaction
            3. close   -- status, then COMMIT

        Committing the claim first is what makes the goodbye unrepeatable. If
        this process dies between 1 and 3 the session is left claimed but
        open; the next sweep skips it, because a claimed session is no longer
        a candidate. It is then closed by the reopen path instead -- the
        customer's next message finds an open session and simply continues it,
        which is a strictly better failure than a duplicate goodbye.

        Returns 0 without touching the database when the lifecycle is off, or
        when CONVERSATION_CLOSE_AFTER_IDLE says idle sessions should simply be
        left open. Note that the two differ: the second still tracks the idle
        timer and still reports WAITING_IDLE, it just never acts on it.
        """
        if not self.enabled:
            return 0
        if not self._settings.conversation_close_after_idle:
            return 0

        idle_before = datetime.now(UTC) - self._settings.conversation_idle_timeout
        claimed = await self._conversations.claim_idle_sessions(
            idle_before, limit=SWEEP_BATCH_SIZE
        )
        if not claimed:
            return 0

        targets = await self._conversations.idle_targets(claimed)
        # Durable before a single message goes out.
        await self._session.commit()

        for target in targets:
            await self._finish(target.conversation_id, target.wa_id)

        logger.info("idle_sessions_closed", count=len(targets))
        return len(targets)

    async def _finish(self, conversation_id: int, wa_id: str) -> None:
        """Send the goodbye if it is allowed, then close the session."""
        if await self._should_send_closing(conversation_id):
            await self._send_closing(conversation_id, wa_id)

        await self._conversations.close(conversation_id)
        await self._session.commit()

    async def _should_send_closing(self, conversation_id: int) -> bool:
        """Whether a goodbye may actually be sent to this session.

        Two reasons it may not, and both still close the session silently.

        The feature can simply be switched off, in which case sessions are
        still bounded -- the next message starts a fresh one and is greeted --
        they just end without a parting message.

        The other is Meta's 24-hour customer service window, and it is not a
        theoretical concern: it is exactly what happens the first time this
        migration runs against an existing database, and after any outage
        longer than a day. Every conversation left open for months becomes
        eligible at once, and without this check the sweeper would attempt a
        send for each one, be rejected by Meta each time, and turn a routine
        deploy into thousands of failing API calls.

        Note that PREVENT_DUPLICATE_CLOSING is not consulted here. The
        guarantee it names is structural rather than conditional: this method
        only ever runs for an id that ``claim_idle_sessions`` has already won
        with a committed conditional UPDATE, so a second goodbye cannot be
        reached to be suppressed. The flag documents the promise; the claim
        keeps it.
        """
        if not self._settings.enable_conversation_closing_message:
            return False
        if not self.closing_text.strip():
            return False
        if self._whatsapp is None:  # pragma: no cover - not wired for sending
            return False

        last_inbound = await self._messages.last_inbound_at(conversation_id)
        if last_inbound is None:
            # Nothing was ever received here, so there is no open window and
            # nobody expecting a reply.
            return False
        if datetime.now(UTC) - last_inbound > CUSTOMER_SERVICE_WINDOW:
            logger.info(
                "closing_skipped_outside_service_window",
                conversation_id=conversation_id,
            )
            return False
        return True

    async def _send_closing(self, conversation_id: int, wa_id: str) -> None:
        """Deliver the closing copy and record it in the transcript.

        A failed send is logged and swallowed. The claim has already been
        committed, so this is never retried -- and that is the intended
        behaviour rather than a gap: retrying a send whose outcome is unknown
        is precisely how a customer gets thanked for their enquiry twice.
        """
        text = self.closing_text
        try:
            result = await self._whatsapp.send_text(wa_id, text)  # type: ignore[union-attr]
        except ExternalServiceError as exc:
            logger.error(
                "closing_send_failed",
                conversation_id=conversation_id,
                error=str(exc),
            )
            return

        wa_message_id = (result.get("messages") or [{}])[0].get("id")
        await self._messages.create(
            conversation_id=conversation_id,
            direction="outbound",
            content=text,
            wa_message_id=wa_message_id,
            status="sent",
        )
        logger.info("closing_message_sent", conversation_id=conversation_id)
        # inbound=False: refresh any dashboard showing this conversation
        # without pulling an operator's attention to it. Nobody is waiting.
        await publish(
            conversation_activity(conversation_id=conversation_id, inbound=False),
            self._settings,
        )
