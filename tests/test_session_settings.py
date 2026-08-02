"""Configuration for the conversation session lifecycle.

Every duration and every behaviour in the lifecycle is a setting; nothing in
app/services/session_service.py holds a number of its own. These tests pin the
documented defaults and the two derived properties, so a change to either has
to be deliberate rather than incidental.

Pure configuration -- no database, no Redis, no event loop. ``_env_file=None``
keeps a developer's real .env out of the assertions, which is what makes the
defaults here mean "what a fresh deployment gets".
"""

from datetime import timedelta

from app.config import Settings


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


class TestDefaults:
    """A deployment that sets none of these gets the documented behaviour."""

    def test_lifecycle_is_on_by_default(self) -> None:
        assert make_settings().enable_conversation_session is True

    def test_documented_defaults(self) -> None:
        settings = make_settings()
        assert settings.conversation_idle_timeout_minutes == 5
        assert settings.conversation_reopen_window_minutes == 30
        assert settings.new_session_after_hours == 24

    def test_behaviour_flags_default_on(self) -> None:
        settings = make_settings()
        assert settings.enable_conversation_closing_message is True
        assert settings.conversation_close_after_idle is True
        assert settings.enable_welcome_on_new_session is True
        assert settings.prevent_duplicate_welcome is True
        assert settings.prevent_duplicate_closing is True
        assert settings.reset_idle_timer_on_outgoing_message is True

    def test_closing_copy_defers_to_the_persona(self) -> None:
        # Empty means "use app/services/persona.py", the same way an unset
        # SYSTEM_PROMPT does. It does not mean "send nothing".
        assert make_settings().conversation_closing_message == ""


class TestIdleTimeout:
    def test_reads_the_configured_value(self) -> None:
        settings = make_settings(conversation_idle_timeout_minutes=17)
        assert settings.conversation_idle_timeout == timedelta(minutes=17)

    def test_floors_at_one_minute(self) -> None:
        # Zero would mark every conversation idle the instant it was created,
        # including one whose reply is still being generated.
        for value in (0, -5):
            settings = make_settings(conversation_idle_timeout_minutes=value)
            assert settings.conversation_idle_timeout == timedelta(minutes=1)


class TestReopenWindow:
    def test_reads_the_configured_value(self) -> None:
        settings = make_settings(conversation_reopen_window_minutes=45)
        assert settings.conversation_reopen_window == timedelta(minutes=45)

    def test_zero_disables_reopen(self) -> None:
        # Unlike the idle timeout, zero is meaningful here: every closed
        # session is final. So this floors at zero, not at one minute.
        settings = make_settings(conversation_reopen_window_minutes=0)
        assert settings.conversation_reopen_window == timedelta(0)

    def test_negative_is_read_as_disabled(self) -> None:
        settings = make_settings(conversation_reopen_window_minutes=-10)
        assert settings.conversation_reopen_window == timedelta(0)


class TestNewSessionAfter:
    def test_reads_the_configured_value(self) -> None:
        settings = make_settings(new_session_after_hours=6)
        assert settings.new_session_after == timedelta(hours=6)

    def test_never_shorter_than_the_reopen_window(self) -> None:
        # The two settings can be configured to contradict each other. Rather
        # than let the outcome depend on which check runs first, the wider one
        # is clamped to the stricter -- erring towards a new session, whose
        # cost is one extra welcome.
        settings = make_settings(
            conversation_reopen_window_minutes=180,
            new_session_after_hours=1,
        )
        assert settings.new_session_after == timedelta(minutes=180)

    def test_zero_hours_with_reopen_disabled_means_always_new(self) -> None:
        settings = make_settings(
            conversation_reopen_window_minutes=0,
            new_session_after_hours=0,
        )
        assert settings.new_session_after == timedelta(0)


class TestEnvironmentOverrides:
    """Changing behaviour must require nothing but a .env edit."""

    def test_every_flag_can_be_switched_off(self, monkeypatch) -> None:
        for name in (
            "ENABLE_CONVERSATION_SESSION",
            "ENABLE_CONVERSATION_CLOSING_MESSAGE",
            "CONVERSATION_CLOSE_AFTER_IDLE",
            "ENABLE_WELCOME_ON_NEW_SESSION",
            "ENABLE_REPEAT_WELCOME_AFTER_NEW_SESSION",
            "PREVENT_DUPLICATE_WELCOME",
            "PREVENT_DUPLICATE_CLOSING",
            "RESET_IDLE_TIMER_ON_OUTGOING_MESSAGE",
        ):
            monkeypatch.setenv(name, "false")

        settings = make_settings()
        assert settings.enable_conversation_session is False
        assert settings.enable_conversation_closing_message is False
        assert settings.conversation_close_after_idle is False
        assert settings.enable_welcome_on_new_session is False
        assert settings.enable_repeat_welcome_after_new_session is False
        assert settings.prevent_duplicate_welcome is False
        assert settings.prevent_duplicate_closing is False
        assert settings.reset_idle_timer_on_outgoing_message is False

    def test_durations_come_from_the_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("CONVERSATION_IDLE_TIMEOUT_MINUTES", "12")
        monkeypatch.setenv("CONVERSATION_REOPEN_WINDOW_MINUTES", "90")
        monkeypatch.setenv("NEW_SESSION_AFTER_HOURS", "48")

        settings = make_settings()
        assert settings.conversation_idle_timeout == timedelta(minutes=12)
        assert settings.conversation_reopen_window == timedelta(minutes=90)
        assert settings.new_session_after == timedelta(hours=48)

    def test_closing_copy_can_be_replaced(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "CONVERSATION_CLOSING_MESSAGE",
            "Thank you for contacting Al Kayan Construction & Finishing.",
        )
        settings = make_settings()
        assert settings.conversation_closing_message.startswith("Thank you")
