"""Which channels the idle sweeper may close, and how it reaches them.

Deliberately free of database and broker fixtures, matching
tests/test_session_lifecycle.py: everything asserted here is a pure function
of in-memory values or of the channel registry, so these are fast and cannot
flake.

NOT COVERED HERE: a real Messenger conversation going idle end to end. That
needs Postgres for the atomic claim and for the partial unique index
releasing on close, and conftest's customer fixtures key everything on
``wa_id``, so a Messenger customer needs fixture work of its own. Faking it
with mocks would pass while production broke, which is worse than saying so.

The first section is the load-bearing one. ``SWEEPABLE_CHANNELS`` is a promise
that every channel in it can actually be sent on, and breaking that promise
fails silently in production rather than loudly here: the claim stamps
``closing_sent_at`` before anything goes out and only ever considers rows
where that column is null, so a channel that cannot send leaves its sessions
marked closed-and-greeted forever with nothing delivered.
"""

from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import BaseChannelAdapter
from app.channels.config import ChannelSettings
from app.channels.constants import (
    FACEBOOK_COMMENT,
    INSTAGRAM_COMMENT,
    INSTAGRAM_DM,
    MESSENGER,
    WHATSAPP,
    profile,
)
from app.channels.outbound import outbound_adapter
from app.config import Settings
from app.integrations.whatsapp import WhatsAppClient
from app.repositories.conversation import SWEEPABLE_CHANNELS, IdleSession
from app.services.session_service import AdapterCache, SessionService


def _settings(**overrides: object) -> Settings:
    # _env_file=None so a developer's local .env cannot change the outcome.
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def _channel_settings(**overrides: Any) -> ChannelSettings:
    return ChannelSettings(_env_file=None, **overrides)


def _client() -> WhatsAppClient:
    """A stand-in for the shared Cloud API client.

    Never called: the adapter only stores the reference, and nothing in these
    tests puts a message on the wire.
    """
    return cast(WhatsAppClient, object())


def _service(whatsapp: WhatsAppClient | None = None, **overrides: object):
    """A service with no database behind it.

    Everything exercised below either never touches the session, or returns
    before it would -- see TestUnaddressableCustomer for the one case where
    that ordering is the assertion.
    """
    return SessionService(cast(AsyncSession, None), _settings(**overrides), whatsapp)


class TestSweepableChannels:
    """What the sweeper is allowed to close, and why each answer is that."""

    def test_whatsapp_is_still_swept(self) -> None:
        assert WHATSAPP in SWEEPABLE_CHANNELS

    def test_messenger_is_swept(self) -> None:
        assert MESSENGER in SWEEPABLE_CHANNELS

    def test_public_comment_channels_are_never_swept(self) -> None:
        """A comment thread is public and has no session to bound.

        Closing one would mean posting a goodbye under somebody's post on an
        idle timer, which is the bot talking to itself in front of an
        audience.
        """
        assert FACEBOOK_COMMENT not in SWEEPABLE_CHANNELS
        assert INSTAGRAM_COMMENT not in SWEEPABLE_CHANNELS

    def test_instagram_dm_waits_for_its_adapter(self) -> None:
        """It has sessions and a resolvable recipient; only the transport is
        missing. Adding it here before the adapter exists is precisely the
        silent failure the module comment warns about."""
        assert INSTAGRAM_DM not in SWEEPABLE_CHANNELS

    def test_the_set_is_pinned(self) -> None:
        """Widening this is a decision about live customer traffic, not a
        tidy-up, so it should fail here and be argued for in review."""
        assert SWEEPABLE_CHANNELS == frozenset({WHATSAPP, MESSENGER})

    def test_every_swept_channel_has_a_session_to_close(self) -> None:
        for channel in SWEEPABLE_CHANNELS:
            assert profile(channel).has_session is True, channel

    async def test_every_swept_channel_can_actually_send(self) -> None:
        """The promise the set is making, checked rather than assumed.

        Settings are explicit so this asserts the code's capability rather
        than the developer's .env: a channel that is merely switched off here
        would still be a correct member of the set.
        """
        settings = _channel_settings(
            enable_messenger=True,
            facebook_page_id="page-1",
            facebook_page_access_token="page-token",
        )
        for channel in sorted(SWEEPABLE_CHANNELS):
            adapter = outbound_adapter(
                channel, whatsapp_client=_client(), settings=settings
            )
            try:
                assert adapter.channel == channel
            finally:
                await adapter.aclose()


class TestIdleTarget:
    def test_a_target_carries_its_channel_and_recipient(self) -> None:
        """The sweep used to resolve every claimed id to User.wa_id, which is
        null for everybody who did not arrive on WhatsApp."""
        target = IdleSession(
            conversation_id=7, channel=MESSENGER, recipient_id="psid-1"
        )
        assert target.channel == MESSENGER
        assert target.recipient_id == "psid-1"


class TestAdapterCache:
    """One transport per channel per sweep, failures remembered too."""

    def test_one_adapter_serves_the_whole_sweep(self) -> None:
        """A Messenger adapter owns an httpx client. Building one per session
        would open up to SWEEP_BATCH_SIZE of them in a single pass."""
        service = _service(_client())
        cache: AdapterCache = {}
        first = service._adapter_for(WHATSAPP, cache)
        second = service._adapter_for(WHATSAPP, cache)
        assert first is not None
        assert first is second

    def test_an_unresolvable_channel_is_remembered_not_retried(self) -> None:
        """Otherwise a misconfigured channel logs once per session, and two
        hundred identical warnings is how a real signal gets ignored."""
        service = _service(_client())
        cache: AdapterCache = {}
        assert service._adapter_for("telegram", cache) is None
        assert cache == {"telegram": None}
        assert service._adapter_for("telegram", cache) is None

    def test_a_service_wired_without_a_client_resolves_nothing(self) -> None:
        """ChatService builds a SessionService with no sender, because it only
        needs the welcome rules. It must not be able to send by accident."""
        service = _service()
        cache: AdapterCache = {}
        assert service._adapter_for(WHATSAPP, cache) is None

    async def test_closing_the_cache_skips_channels_that_never_resolved(
        self,
    ) -> None:
        closed: list[str] = []

        class _Recording(BaseChannelAdapter):
            channel = MESSENGER

            def parse(self, payload: dict[str, Any]) -> list[Any]:
                return []

            async def send_text(self, recipient: str, text: str) -> dict[str, Any]:
                return {}

            async def aclose(self) -> None:
                closed.append(self.channel)

        cache: AdapterCache = {MESSENGER: _Recording(), "telegram": None}
        await _service()._close_adapters(cache)
        assert closed == [MESSENGER]

    async def test_a_transport_that_fails_to_close_does_not_fail_the_sweep(
        self,
    ) -> None:
        """The sessions are already closed by this point. Raising here would
        turn a completed sweep into a retried one."""

        class _Stuck(BaseChannelAdapter):
            channel = MESSENGER

            def parse(self, payload: dict[str, Any]) -> list[Any]:
                return []

            async def send_text(self, recipient: str, text: str) -> dict[str, Any]:
                return {}

            async def aclose(self) -> None:
                raise RuntimeError("connection already gone")

        await _service()._close_adapters({MESSENGER: _Stuck()})


class TestUnaddressableCustomer:
    async def test_a_customer_with_no_id_is_never_sent_to(self) -> None:
        """Returns False before reaching the database, which is the point: the
        alternative is a send to the empty string. The session still closes --
        the claim is committed and one-way, so leaving it open would strand it
        permanently.

        The absence of a database here is load-bearing. This service has no
        session behind it, so a check that ran in the wrong order would raise
        rather than quietly pass.
        """
        service = _service(_client(), enable_conversation_closing_message=True)
        target = IdleSession(
            conversation_id=7, channel=MESSENGER, recipient_id=None
        )
        assert await service._should_send_closing(target) is False
