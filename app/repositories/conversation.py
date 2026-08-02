"""Conversation data access."""

from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple, Sequence

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.core.exceptions import ConflictError
from app.models.conversation import (
    MODE_BOT,
    MODE_HUMAN,
    STATUS_ACTIVE,
    STATUS_CLOSED,
    TAG_SALES_LEAD,
    Conversation,
)
from app.models.user import User
from app.repositories.base import BaseRepository

# A sales lead nobody has claimed yet sorts above everything else. All four
# conditions matter:
#
#   status = active    -- a closed session is nobody's outstanding work. This
#                         condition was added with the session lifecycle and
#                         is load-bearing: before sessions closed themselves a
#                         lead stayed active until an operator claimed it, but
#                         now it closes after five idle minutes and without
#                         this it would stay pinned to the top of every
#                         operator's screen forever -- precisely the failure
#                         the rest of this comment was written to prevent.
#   tag                -- only leads jump the queue
#   mode = human       -- a conversation the AI has resumed is handled
#   no operator        -- once someone presses Take Over it is theirs, and
#                         leaving it pinned would keep drawing other operators
#                         to a row that is already being answered
#
# Without these the row would stay at the top of every operator's screen
# forever, which trains people to ignore the top of the list.
_UNCLAIMED_LEAD = (
    (Conversation.status == STATUS_ACTIVE)
    & (Conversation.tag == TAG_SALES_LEAD)
    & (Conversation.mode == MODE_HUMAN)
    & (Conversation.assigned_operator.is_(None))
)

_UNCLAIMED_LEAD_FIRST = case((_UNCLAIMED_LEAD, 0), else_=1)

# What it means to revive a closed session, in one place.
#
# Two callers need this -- a customer coming back inside the reopen window,
# and an operator acting on a session the sweeper already closed -- and they
# must agree exactly, because the difference between them would be silent:
#
#   status         -- back to active, which retakes the customer's slot in
#                     uq_active_conversation_per_user
#   closed_at      -- cleared; the session has not ended
#   closing_sent_at-- cleared, which RE-ARMS the goodbye. Without this the
#                     revived session could never be closed again, because
#                     claim_idle_sessions only ever considers rows where this
#                     column is null.
#
# welcome_sent_at is conspicuously absent, and that is the point: it survives,
# so should_welcome() already returns False for a revived session and no
# caller has to remember to suppress a second greeting. History survives for
# the same reason -- the messages hang off this row and it is the same row.
_REVIVE: dict[str, Any] = {
    "status": STATUS_ACTIVE,
    "closed_at": None,
    "closing_sent_at": None,
}


class IdleSession(NamedTuple):
    """A session claimed for closing, with what the sender needs to act."""

    conversation_id: int
    wa_id: str


class ConversationRepository(BaseRepository):
    async def get(self, conversation_id: int) -> Conversation | None:
        return await self.session.get(Conversation, conversation_id)

    async def get_with_messages(self, conversation_id: int) -> Conversation | None:
        return await self.session.scalar(
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .options(selectinload(Conversation.messages))
        )

    async def active_for_user(self, user_id: int) -> Conversation | None:
        """The customer's open conversation, if any.

        Deliberately not filtered by ``mode``: a conversation owned by a human
        operator is still the customer's open conversation. Filtering it out
        here would make the next inbound message create a second one.

        It IS filtered by ``status``, which is what makes a closed session
        reopen cleanly: once the sweeper sets status to ``closed`` this returns
        nothing, and ``get_or_create_active`` mints a fresh row.
        """
        return await self.session.scalar(
            select(Conversation)
            .where(
                Conversation.user_id == user_id,
                Conversation.status == STATUS_ACTIVE,
            )
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )

    async def _revive(self, criteria: Sequence[ColumnElement[bool]]) -> int | None:
        """Apply :data:`_REVIVE` to the one row matching ``criteria``.

        Shared by both reopen paths so that reviving a session means exactly
        the same thing however it is triggered.

        The conditional UPDATE and the nested transaction are both about the
        partial unique index. Two concurrent callers could each find no active
        conversation for a customer and both try to revive a row, and the
        second would violate ``uq_active_conversation_per_user``. Rolling back
        only the savepoint lets the loser fall through and re-read the
        winner's row rather than poisoning the whole surrounding transaction --
        which matters because one of those callers is holding an inbound
        message that still has to be committed.

        Returns the revived id, or ``None`` if nothing matched or somebody
        else won the race.
        """
        try:
            async with self.session.begin_nested():
                return await self.session.scalar(
                    update(Conversation)
                    .where(*criteria, Conversation.status == STATUS_CLOSED)
                    .values(**_REVIVE, last_activity_at=datetime.now(UTC))
                    .returning(Conversation.id)
                    .execution_options(synchronize_session=False)
                )
        except IntegrityError:
            # Someone else opened a session for this customer first.
            return None

    async def reopen_recent(
        self, user_id: int, not_before: datetime
    ) -> Conversation | None:
        """Resume the customer's most recently closed session, if it is young.

        A goodbye followed thirty seconds later by "sorry, one more thing" is
        one conversation, not two. Starting a fresh session there would greet
        the customer again and drop the history the model needs to understand
        what "it" refers to, so within the reopen window the old session is
        revived instead.

        Returns ``None`` when there is nothing recent enough, and the caller
        opens a new session as usual.
        """
        candidate = (
            select(Conversation.id)
            .where(
                Conversation.user_id == user_id,
                Conversation.status == STATUS_CLOSED,
                Conversation.closed_at.is_not(None),
                Conversation.closed_at >= not_before,
            )
            .order_by(Conversation.closed_at.desc())
            .limit(1)
            .scalar_subquery()
        )
        reopened_id = await self._revive([Conversation.id == candidate])
        if reopened_id is None:
            return None
        return await self.session.get(Conversation, reopened_id)

    async def reopen(self, conversation_id: int) -> Conversation | None:
        """Revive one specific closed session, regardless of its age.

        For operator actions. An operator replying to, or taking over, a
        session the sweeper has already closed used to write into a dead row:
        the message went out, but the customer's answer either revived a
        different session or created one, so the operator's question and the
        reply to it ended up in two separate conversations. Reviving first
        keeps the exchange in one place.

        No window is applied on purpose. The reopen window exists to decide
        whether a returning CUSTOMER is continuing their visit or starting a
        new one, which is a guess about intent. An operator deliberately
        opening a specific conversation and replying to it has stated their
        intent, and second-guessing it after an arbitrary number of minutes
        would just resurrect the orphaned-reply bug for older sessions.

        Returns ``None`` when the customer has since started another session,
        because the partial unique index permits only one active conversation
        per customer. That is not a failure to handle quietly: the caller must
        refuse the action, since writing into this row would put the operator's
        message somewhere the customer is no longer reading.
        """
        reopened_id = await self._revive([Conversation.id == conversation_id])
        if reopened_id is None:
            return None
        conversation = await self.session.get(Conversation, reopened_id)
        if conversation is not None:
            await self.session.refresh(conversation)
        return conversation

    async def get_or_create_active(
        self, user_id: int, reopen_within: timedelta | None = None
    ) -> Conversation:
        """Return the customer's active conversation, creating it atomically.

        Concurrent webhook deliveries used to be able to create two active
        conversations for one customer, which then split the history in half
        and halved the context the model saw. Migration
        0003_search_and_concurrency adds the partial unique index
        ``uq_active_conversation_per_user``; inferring that index in
        ``ON CONFLICT DO NOTHING`` makes the create side atomic and lets the
        loser re-read the winner's row.

        That index is partial on ``status = 'active'``, which is also what
        makes session reopen free: a closed session no longer occupies the
        customer's slot, so the next message opens a new conversation with a
        null welcome flag, a null closing flag and no history -- nothing to
        reset, and therefore nothing to forget to reset.

        ``reopen_within`` softens exactly that behaviour for a customer who
        comes straight back: within it the previous session is revived instead
        (see :meth:`reopen_recent`). ``None`` or zero keeps the original
        always-start-fresh path, so every existing caller is unaffected.

        ``mode`` is not listed in the insert, so it comes from the column's
        server default rather than the ORM default. The same is true of
        ``last_activity_at``, which is why that column carries a server
        default of now().
        """
        conversation = await self.active_for_user(user_id)
        if conversation is not None:
            return conversation

        if reopen_within is not None and reopen_within > timedelta(0):
            resumed = await self.reopen_recent(
                user_id, datetime.now(UTC) - reopen_within
            )
            if resumed is not None:
                return resumed

        created_id = await self.session.scalar(
            pg_insert(Conversation)
            .values(user_id=user_id, status=STATUS_ACTIVE)
            .on_conflict_do_nothing(
                index_elements=[Conversation.user_id],
                index_where=Conversation.status == STATUS_ACTIVE,
            )
            .returning(Conversation.id)
        )
        if created_id is not None:
            created = await self.session.get(Conversation, created_id)
            if created is not None:
                return created

        conversation = await self.active_for_user(user_id)
        if conversation is None:  # pragma: no cover - only if the winner rolled back
            raise ConflictError(
                f"Could not open an active conversation for user {user_id}"
            )
        return conversation

    async def set_mode(
        self,
        conversation: Conversation,
        mode: str,
        operator: str | None = None,
        tag: str | None = None,
    ) -> Conversation:
        """Switch who answers this conversation.

        ``handoff_at`` records when the CURRENT handoff began, so it is cleared
        when the AI resumes; the same applies to ``assigned_operator``. Neither
        is an audit log -- the transitions are in the structured logs, and a
        real history would need its own table.

        ``tag`` behaves differently from both: it is only ever set, never
        cleared. A conversation that produced a sales lead produced one, even
        after the AI takes it back, and clearing it would make the lead vanish
        from tomorrow's report. Passing ``None`` leaves any existing tag alone
        rather than erasing it, so an ordinary later handoff cannot silently
        downgrade a conversation that was already classified.

        Switching direction is activity, so it resets the idle timer. Without
        that, a conversation handed back to the AI after twenty quiet minutes
        with an operator would be swept and closed on the very next pass,
        before the customer had any chance to notice the AI was back.

        Does not commit: the caller owns the transaction boundary, because a
        handoff triggered by an inbound message must be committed together with
        that message.
        """
        conversation.mode = mode
        if tag is not None:
            conversation.tag = tag
        if mode == MODE_HUMAN:
            conversation.assigned_operator = operator
            conversation.handoff_at = datetime.now(UTC)
        else:
            conversation.assigned_operator = None
            conversation.handoff_at = None
        conversation.last_activity_at = datetime.now(UTC)
        await self.session.flush()
        return conversation

    async def touch(self, conversation_id: int) -> None:
        """Reset the idle timer for this conversation.

        Called for every inbound message, every reply in either direction and
        every switch between bot and human. An UPDATE by id rather than a
        mutation of a loaded object, so callers that never loaded the row (the
        send path holds only an id) do not have to fetch it first.

        Does not commit. Every caller is already inside a transaction that is
        about to commit for its own reasons, and the timer must land or not
        land together with the message that reset it -- a committed reply with
        a rolled-back timer is a conversation that closes while it is being
        answered.
        """
        await self.session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(last_activity_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )

    async def mark_welcome_sent(self, conversation_id: int) -> None:
        """Record that this session has greeted its customer.

        Conditional on the column still being null so that a redelivery cannot
        move the timestamp forward; the value is read as a boolean today, but
        a moving timestamp would quietly corrupt any later report of when
        sessions actually start.
        """
        await self.session.execute(
            update(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.welcome_sent_at.is_(None),
            )
            .values(welcome_sent_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )

    async def count_for_user(self, user_id: int) -> int:
        """How many conversations this customer has ever had.

        Used when ENABLE_REPEAT_WELCOME_AFTER_NEW_SESSION is off, to tell a
        genuinely new customer from a returning one, and by the operator's
        customer history panel.
        """
        return int(
            await self.session.scalar(
                select(func.count(Conversation.id)).where(
                    Conversation.user_id == user_id
                )
            )
            or 0
        )

    async def for_user(
        self, user_id: int, limit: int = 20, exclude_id: int | None = None
    ) -> list[Conversation]:
        """This customer's other sessions, most recent first.

        For the operator's history panel. Sessions stay separate rows -- they
        are separate visits and merging them would misrepresent what happened
        -- but an operator answering someone needs to know they have been here
        four times before, so the panel links across them.

        Explicitly NOT used to build model context: the AI still sees only the
        current session, because silently widening what it remembers would
        change its answers in ways nobody asked for.
        """
        stmt = select(Conversation).where(Conversation.user_id == user_id)
        if exclude_id is not None:
            stmt = stmt.where(Conversation.id != exclude_id)
        result = await self.session.scalars(
            stmt.order_by(Conversation.created_at.desc()).limit(limit)
        )
        return list(result)

    async def claim_idle_sessions(
        self, idle_before: datetime, limit: int = 200
    ) -> list[int]:
        """Take ownership of every session that has gone idle, atomically.

        Returns the ids this caller now owns. Another sweep running at the
        same moment gets a disjoint set, and no session is ever handed to two
        callers -- which is the entire guarantee behind "only one closing
        message can ever be sent per session".

        The claim is a single conditional UPDATE rather than a SELECT followed
        by an UPDATE. That distinction is the whole mechanism: with a read
        first, two workers both see ``closing_sent_at IS NULL``, both decide to
        act, and the customer is told goodbye twice. Here Postgres re-evaluates
        the predicate while holding the row lock, so the second writer matches
        nothing and is told so before it sends anything.

        ``FOR UPDATE SKIP LOCKED`` in the subquery is a throughput detail, not
        a correctness one: it lets a second sweeper walk past rows the first is
        already working on instead of blocking behind them.

        Restricted to ``mode = bot`` on purpose. A conversation an operator has
        taken is somebody's open work, and a bot that says goodbye in the
        middle of it -- possibly between two of the operator's own messages --
        would be worse than no lifecycle at all. The handoff case the spec
        cares about still works: resuming the AI sets mode back to bot and
        resets the timer, so the session becomes eligible from that moment.

        ``limit`` bounds one pass so a long outage cannot produce a single
        enormous transaction; the next tick takes the next batch.
        """
        candidates = (
            select(Conversation.id)
            .where(
                Conversation.status == STATUS_ACTIVE,
                Conversation.mode == MODE_BOT,
                Conversation.closing_sent_at.is_(None),
                Conversation.last_activity_at < idle_before,
            )
            .order_by(Conversation.last_activity_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        result = await self.session.execute(
            update(Conversation)
            .where(
                Conversation.id.in_(candidates),
                Conversation.closing_sent_at.is_(None),
            )
            .values(closing_sent_at=datetime.now(UTC))
            .returning(Conversation.id)
            .execution_options(synchronize_session=False)
        )
        return [row[0] for row in result.all()]

    async def idle_targets(self, conversation_ids: list[int]) -> list[IdleSession]:
        """Resolve claimed session ids to the WhatsApp numbers to send to."""
        if not conversation_ids:
            return []
        rows = await self.session.execute(
            select(Conversation.id, User.wa_id)
            .join(User, User.id == Conversation.user_id)
            .where(Conversation.id.in_(conversation_ids))
        )
        return [IdleSession(conversation_id=row[0], wa_id=row[1]) for row in rows.all()]

    async def close(self, conversation_id: int) -> None:
        """End a session.

        Setting ``status`` to closed releases the customer's slot in
        ``uq_active_conversation_per_user``, so their next message opens a new
        conversation rather than resuming this one -- unless it arrives inside
        the reopen window, in which case :meth:`reopen_recent` revives this
        row instead.
        """
        await self.session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(status=STATUS_CLOSED, closed_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )

    async def list(
        self, offset: int = 0, limit: int = 50, status: str | None = None
    ) -> list[Conversation]:
        """Conversations for the operator list.

        Ordering, in full: unclaimed sales leads first, then everything else
        by recency, newest first.

        Recency deliberately dominates within the second group rather than
        status. Pushing every closed session below every active one sounds
        tidier and is worse in practice -- an operator scanning the list is
        looking for what happened recently, and a conversation that ended four
        minutes ago is far more interesting to them than one that has been
        sitting open and silent since yesterday. Operators who want only live
        work filter for it instead, which is what ``status`` is for.

        Note that only ACTIVE unclaimed leads are pinned; see
        ``_UNCLAIMED_LEAD``. A closed lead is not outstanding work.

        Sorting here rather than in the dashboard means every client -- the web
        UI, the mobile app, anyone reading the admin API -- gets the same
        order, and that pagination stays correct: ordering a page after it has
        been fetched only sorts the fifty rows that happened to be on it.

        ``status`` filters to one lifecycle status. It exists because sessions
        now end: with one row per visit rather than one per customer, an
        operator's live work is a shrinking minority of the table and would
        otherwise be buried under closed history within a day.
        """
        stmt = select(Conversation)
        if status is not None:
            stmt = stmt.where(Conversation.status == status)
        result = await self.session.scalars(
            stmt.order_by(_UNCLAIMED_LEAD_FIRST, Conversation.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result)

    async def delete(self, conversation: Conversation) -> None:
        await self.session.delete(conversation)

    async def count(self, status: str | None = None) -> int:
        """How many conversations exist, optionally within one status.

        The unfiltered count is a count of SESSIONS, not of customers, and has
        been since sessions started closing themselves. Callers that want
        customers should count users.
        """
        stmt = select(func.count(Conversation.id))
        if status is not None:
            stmt = stmt.where(Conversation.status == status)
        return int(await self.session.scalar(stmt) or 0)

    async def count_unclaimed_leads(self) -> int:
        """Open sales leads with nobody on them -- the dashboard badge.

        Scoped to active sessions by ``_UNCLAIMED_LEAD``, so a lead that went
        idle and closed stops inflating the badge. An operator cannot pick up
        a closed session's lead, and a badge counting work nobody can do is a
        badge people learn to ignore.
        """
        return int(
            await self.session.scalar(
                select(func.count(Conversation.id)).where(_UNCLAIMED_LEAD)
            )
            or 0
        )
