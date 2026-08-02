"""Conversation session lifecycle: states, timeout handling and closing copy.

Deliberately free of database and broker fixtures. Everything asserted here is
a pure function of in-memory values, so these tests are fast and cannot flake;
the parts that genuinely need Postgres -- the atomic claim, the partial unique
index releasing on close -- are exercised by the integration suite.
"""

from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.conversation import (
    MODE_BOT,
    MODE_HUMAN,
    SESSION_ACTIVE_BOT,
    SESSION_ACTIVE_HUMAN,
    SESSION_CLOSED,
    SESSION_WAITING_IDLE,
    STATUS_ACTIVE,
    STATUS_CLOSED,
    Conversation,
)
from app.services.persona import CLOSING
from app.services.session_service import SessionService

IDLE_AFTER = timedelta(minutes=5)
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _conversation(
    status: str = STATUS_ACTIVE,
    mode: str = MODE_BOT,
    idle_for: timedelta = timedelta(0),
) -> Conversation:
    return Conversation(
        status=status,
        mode=mode,
        last_activity_at=NOW - idle_for,
    )


def _settings(**overrides: object) -> Settings:
    # _env_file=None so a developer's local .env cannot change the outcome.
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


class TestSessionState:
    def test_recent_bot_conversation_is_active(self) -> None:
        conversation = _conversation(idle_for=timedelta(seconds=30))
        assert conversation.session_state(IDLE_AFTER, NOW) == SESSION_ACTIVE_BOT

    def test_quiet_bot_conversation_is_idle(self) -> None:
        conversation = _conversation(idle_for=timedelta(minutes=6))
        assert conversation.session_state(IDLE_AFTER, NOW) == SESSION_WAITING_IDLE

    def test_idle_is_inclusive_at_the_boundary(self) -> None:
        """Exactly the timeout counts as idle, matching the sweeper's own test."""
        conversation = _conversation(idle_for=IDLE_AFTER)
        assert conversation.session_state(IDLE_AFTER, NOW) == SESSION_WAITING_IDLE

    def test_quiet_human_conversation_is_not_idle(self) -> None:
        """A conversation an operator holds is somebody's open work.

        WAITING_IDLE means "due to be closed", and the sweeper never closes a
        human conversation. If this reported idle, the state an operator reads
        would contradict what the closing logic actually does.
        """
        conversation = _conversation(mode=MODE_HUMAN, idle_for=timedelta(hours=3))
        assert conversation.session_state(IDLE_AFTER, NOW) == SESSION_ACTIVE_HUMAN

    def test_closed_beats_everything(self) -> None:
        conversation = _conversation(
            status=STATUS_CLOSED, mode=MODE_HUMAN, idle_for=timedelta(0)
        )
        assert conversation.session_state(IDLE_AFTER, NOW) == SESSION_CLOSED

    def test_is_open_tracks_status(self) -> None:
        assert _conversation().is_open is True
        assert _conversation(status=STATUS_CLOSED).is_open is False


class TestIdleTimeoutSetting:
    def test_reads_the_configured_value(self) -> None:
        assert _settings(
            conversation_idle_timeout_minutes=15
        ).conversation_idle_timeout == timedelta(minutes=15)

    def test_zero_is_floored_to_one_minute(self) -> None:
        """A zero timeout would mark every conversation idle the instant it was
        created -- including one whose reply is still being generated, so the
        customer would be told goodbye before they were answered."""
        assert _settings(
            conversation_idle_timeout_minutes=0
        ).conversation_idle_timeout == timedelta(minutes=1)

    def test_negative_is_floored_too(self) -> None:
        assert _settings(
            conversation_idle_timeout_minutes=-5
        ).conversation_idle_timeout == timedelta(minutes=1)


class TestClosingText:
    def _service(self, settings: Settings) -> SessionService:
        # closing_text never touches the session; the repositories it builds
        # only store the reference.
        return SessionService(cast(AsyncSession, None), settings)

    def test_falls_back_to_the_persona_copy(self) -> None:
        assert self._service(_settings()).closing_text == CLOSING

    def test_blank_setting_falls_back(self) -> None:
        """An empty CONVERSATION_CLOSING_MESSAGE= line must not silence the
        goodbye -- that is the default state of .env.example."""
        service = self._service(_settings(conversation_closing_message="   "))
        assert service.closing_text == CLOSING

    def test_configured_value_wins(self) -> None:
        service = self._service(
            _settings(conversation_closing_message="Thank you for contacting us.")
        )
        assert service.closing_text == "Thank you for contacting us."


class TestClosingCopy:
    def test_is_written_in_real_arabic(self) -> None:
        """persona.py requires real Arabic, not escapes.

        Escaped codepoints cannot be proofread by the person who owns the
        wording, and a previous regression that escaped this file also broke
        formatting -- black measures source-text length, and each Arabic
        character became six source characters.
        """
        assert "\\u" not in CLOSING
        assert any("\u0621" <= character <= "\u064a" for character in CLOSING)

    def test_invites_the_customer_back(self) -> None:
        """The goodbye must not read as a door closing: the next thing many
        customers do is write again, and that reopens the session."""
        assert CLOSING.strip()
        assert "\u0627\u0644\u0643\u064a\u0627\u0646" in CLOSING
