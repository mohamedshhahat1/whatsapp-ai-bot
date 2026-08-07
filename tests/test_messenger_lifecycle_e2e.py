"""A Messenger session, from first message to being greeted a second time.

The companion file tests/test_messenger_lifecycle.py is deliberately free of
database fixtures and says so; it also recorded what it could not cover:

    NOT COVERED HERE: a real Messenger conversation going idle end to end.
    That needs Postgres for the atomic claim and for the partial unique index
    releasing on close [...] Faking it with mocks would pass while production
    broke, which is worse than saying so.

This is that test. It runs against the real schema, so the claim is the real
conditional UPDATE and the new session is minted by the real partial unique
index rather than by a stub agreeing with the code.

The re-welcome is the reason this exists. It is not implemented anywhere --
no branch decides to greet somebody again. It happens because closing a
session releases the customer's slot in uq_active_conversation_per_user, so
their next message creates a row whose welcome_sent_at is null, and
should_welcome reads that one column. Behaviour that emerges from two
unrelated mechanisms agreeing is behaviour no reader can verify by looking at
one file, and is exactly what a later refactor breaks quietly.

Only the transport is faked. The adapter is replaced so nothing reaches
Meta, and publish is replaced so nothing reaches Redis; every row, index and
constraint below is real.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import BaseChannelAdapter
from app.channels.constants import MESSENGER
from app.config import Settings
from app.integrations.whatsapp import WhatsAppClient
from app.models.conversation import STATUS_ACTIVE, STATUS_CLOSED, Conversation
from app.repositories.conversation import ConversationRepository
from app.repositories.message import MessageRepository
from app.services import session_service as session_service_module
from app.services.persona import CLOSING
from app.services.session_service import SessionService
from tests.conftest import Customer

IDLE_MINUTES = 5


class _RecordingAdapter(BaseChannelAdapter):
    """Stands in for the Graph API and remembers what it was asked to send."""

    channel = MESSENGER

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.closed = False

    def parse(self, payload: dict[str, Any]) -> list[Any]:
        return []

    async def send_text(self, recipient: str, text: str) -> dict[str, Any]:
        self.sent.append((recipient, text))
        # Derived from the recipient rather than a literal: messages.
        # wa_message_id is uniquely indexed, so a constant here would collide
        # the second time any test in this file sends something.
        return {"message_id": f"mid.out.{recipient}"}

    async def aclose(self) -> None:
        self.closed = True


def _settings(**overrides: object) -> Settings:
    # _env_file=None so a developer's local .env cannot change the outcome.
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def _lifecycle_settings(**overrides: object) -> Settings:
    """Every switch the lifecycle reads, set explicitly.

    Stated rather than inherited so this asserts what the code does, not what
    the developer's environment happens to enable.
    """
    defaults: dict[str, object] = {
        "enable_conversation_session": True,
        "conversation_close_after_idle": True,
        "enable_conversation_closing_message": True,
        "enable_welcome_on_new_session": True,
        "enable_repeat_welcome_after_new_session": True,
        "prevent_duplicate_welcome": True,
        "conversation_idle_timeout_minutes": IDLE_MINUTES,
    }
    defaults.update(overrides)
    return _settings(**defaults)


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> _RecordingAdapter:
    """Replace the transport, and the event bus, for the whole sweep."""
    recorder = _RecordingAdapter()
    monkeypatch.setattr(
        session_service_module,
        "outbound_adapter",
        lambda channel, **kwargs: recorder,
    )
    return recorder


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Collect events instead of putting them on Redis."""
    events: list[Any] = []

    async def _record(event: Any, settings: Any) -> None:
        events.append(event)

    monkeypatch.setattr(session_service_module, "publish", _record)
    return events


def _service(session: AsyncSession, **overrides: object) -> SessionService:
    """A service wired for sending.

    The WhatsApp client is a bare object and is never called: _adapter_for
    only checks it is not None before resolving, and the resolver is patched.
    """
    return SessionService(
        session,
        _lifecycle_settings(**overrides),
        cast(WhatsAppClient, object()),
    )


async def _go_quiet(session: AsyncSession, conversation_id: int) -> None:
    """Backdate the idle timer well past the timeout."""
    await session.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(last_activity_at=datetime.now(UTC) - timedelta(minutes=60))
    )
    await session.commit()


async def _arrive(session: AsyncSession, customer: Customer, suffix: str = "1") -> None:
    """Record an inbound message and mark the session greeted.

    The inbound row is not decoration: _should_send_closing refuses to send
    into a conversation that has never received anything, and it checks the
    24-hour service window against this timestamp.
    """
    await MessageRepository(session).create(
        conversation_id=customer.conversation_id,
        direction="inbound",
        content="\u0645\u0645\u0643\u0646 \u0627\u0644\u0623\u0633\u0639\u0627\u0631\u061f",
        wa_message_id=f"mid.in.{customer.external_id}.{suffix}",
    )
    await ConversationRepository(session).mark_welcome_sent(customer.conversation_id)
    await session.commit()


class TestMessengerRoundTrip:
    async def test_idle_closes_says_goodbye_and_greets_again_next_time(
        self,
        db: AsyncSession,
        messenger_customer: Customer,
        adapter: _RecordingAdapter,
        published: list[Any],
    ) -> None:
        """The whole lifecycle, in the order the customer experiences it."""
        conversations = ConversationRepository(db)
        await _arrive(db, messenger_customer)

        first = await conversations.get(messenger_customer.conversation_id)
        assert first is not None
        assert first.channel == MESSENGER
        assert first.status == STATUS_ACTIVE

        service = _service(db)
        # Already greeted, so not owed another one inside this session.
        assert await service.should_welcome(first) is False

        # 1. The session goes idle.
        await _go_quiet(db, messenger_customer.conversation_id)

        # 2. The sweep claims it and says goodbye.
        #
        # Membership rather than equality throughout: the sweep is global, so
        # anything else left idle in the database is claimed by the same pass.
        # The row-level assertions below are exact, and they are the ones that
        # matter.
        closed_count = await service.close_idle_sessions()
        assert closed_count >= 1
        assert (messenger_customer.external_id, CLOSING) in adapter.sent
        assert adapter.closed is True

        # The goodbye is addressed to the page-scoped id. Before the channel
        # work this resolved to User.wa_id, which is null here.
        recipients = [recipient for recipient, _ in adapter.sent]
        assert messenger_customer.external_id in recipients

        # 3. The session is closed, and the dashboard was told.
        await db.refresh(first)
        assert first.status == STATUS_CLOSED
        assert first.closed_at is not None
        assert first.closing_sent_at is not None
        assert published, "closing the session must announce it"

        # The goodbye is in the transcript, so an operator opening the
        # conversation sees what the customer saw.
        transcript = await MessageRepository(db).recent(first.id)
        assert CLOSING in [message.content for message in transcript]

        # 4. The next message opens a NEW session rather than resuming.
        second = await conversations.get_or_create_active(
            messenger_customer.user_id, channel=MESSENGER
        )
        await db.commit()
        assert second.id != first.id
        assert second.channel == MESSENGER
        assert second.status == STATUS_ACTIVE

        # 5. And the customer is greeted again -- because the new row's
        #    welcome flag is null, not because anything decided to re-greet.
        assert second.welcome_sent_at is None
        assert await service.should_welcome(second) is True

    async def test_a_customer_who_comes_straight_back_is_not_greeted_twice(
        self,
        db: AsyncSession,
        messenger_customer: Customer,
        adapter: _RecordingAdapter,
        published: list[Any],
    ) -> None:
        """The opposite outcome from the same starting point.

        A goodbye followed thirty seconds later by "sorry, one more thing" is
        one visit. Inside the reopen window the closed session is revived
        rather than replaced, welcome_sent_at survives _REVIVE, and greeting
        them again would read as the bot having forgotten the conversation it
        was just having.
        """
        conversations = ConversationRepository(db)
        await _arrive(db, messenger_customer)
        await _go_quiet(db, messenger_customer.conversation_id)

        service = _service(db)
        await service.close_idle_sessions()

        resumed = await conversations.get_or_create_active(
            messenger_customer.user_id,
            reopen_within=timedelta(minutes=30),
            channel=MESSENGER,
        )
        await db.commit()

        assert resumed.id == messenger_customer.conversation_id
        assert resumed.status == STATUS_ACTIVE
        # Re-armed, or this session could never be closed a second time.
        assert resumed.closing_sent_at is None
        assert resumed.welcome_sent_at is not None
        assert await service.should_welcome(resumed) is False

    async def test_a_session_still_closes_when_the_goodbye_is_switched_off(
        self,
        db: AsyncSession,
        messenger_customer: Customer,
        adapter: _RecordingAdapter,
        published: list[Any],
    ) -> None:
        """Sessions stay bounded with ENABLE_CONVERSATION_CLOSING_MESSAGE off.

        They just end without a parting message. The close still has to be
        announced: the conversation list stops polling while its event stream
        is connected, so a healthy dashboard is precisely the one that would
        sit showing a stale "active" row.
        """
        await _arrive(db, messenger_customer)
        await _go_quiet(db, messenger_customer.conversation_id)

        service = _service(db, enable_conversation_closing_message=False)
        await service.close_idle_sessions()

        conversation = await ConversationRepository(db).get(
            messenger_customer.conversation_id
        )
        assert conversation is not None
        assert conversation.status == STATUS_CLOSED
        assert adapter.sent == []
        assert published, "a silent close is still a close the dashboard needs"
