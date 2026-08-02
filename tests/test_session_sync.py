"""Regression cover for the backend/dashboard/mobile synchronisation pass.

Scope, stated plainly so nobody mistakes a green run here for more than it is.

COVERED: everything that is a pure function of in-memory values -- the derived
session state, the configured return-window boundaries, the shared revive
contract, and the exact shape of the lifecycle events the two clients consume.
These are fast and cannot flake.

NOT COVERED HERE: anything that needs a real database. The atomic claim in
``claim_idle_sessions``, the partial unique index releasing on close, and the
savepoint rollback in ``_revive`` are all Postgres behaviours -- SQLite and
mocks would happily pass while production broke, which is worse than no test.
They belong in the integration suite.

The boundary tests below assert the *policy* (what the configured windows are
and which side of them a given absence falls on), not the SQL that applies it.
That is a real limitation: a query that ignored the window entirely would
still pass these.
"""

from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.core.events import (
    CLOSED,
    REOPENED,
    conversation_closed,
    conversation_reopened,
)
from app.models.conversation import (
    MODE_BOT,
    MODE_HUMAN,
    SESSION_ACTIVE_BOT,
    SESSION_ACTIVE_HUMAN,
    SESSION_CLOSED,
    SESSION_CLOSING,
    SESSION_WAITING_IDLE,
    STATUS_ACTIVE,
    STATUS_CLOSED,
    derive_session_state,
)
from app.repositories.conversation import _REVIVE
from app.services.reply_service import ConversationSupersededError

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
IDLE_AFTER = timedelta(minutes=5)


def _settings(**overrides: object) -> Settings:
    # _env_file=None so a developer's local .env cannot change the outcome.
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def _state(
    *,
    status: str = STATUS_ACTIVE,
    mode: str = MODE_BOT,
    idle_for: timedelta = timedelta(0),
    closing_sent_at: datetime | None = None,
) -> str:
    return derive_session_state(
        status=status,
        mode=mode,
        last_activity_at=NOW - idle_for,
        closing_sent_at=closing_sent_at,
        idle_after=IDLE_AFTER,
        now=NOW,
    )


class TestDerivedSessionState:
    """One function derives the state for the API, dashboard and mobile app.

    It is a free function rather than a method so the schema layer can call it
    without loading an ORM object, which is what stops the three clients from
    each growing their own slightly different version.
    """

    def test_fresh_bot_session_is_active(self) -> None:
        assert _state(idle_for=timedelta(seconds=30)) == SESSION_ACTIVE_BOT

    def test_quiet_bot_session_is_waiting_idle(self) -> None:
        assert _state(idle_for=timedelta(minutes=6)) == SESSION_WAITING_IDLE

    def test_operator_owned_session_is_never_waiting_idle(self) -> None:
        """The sweeper only closes bot conversations, so reporting an
        operator's quiet conversation as due-to-close would show a state that
        contradicts what will actually happen to it."""
        assert _state(mode=MODE_HUMAN, idle_for=timedelta(hours=3)) == (
            SESSION_ACTIVE_HUMAN
        )

    def test_claimed_session_reports_closing(self) -> None:
        """closing_sent_at is set the instant the sweeper claims a session,
        before the goodbye is delivered. That gap is short but real, and an
        operator looking at the row during it should see that it is on its way
        out rather than a state that says they can still jump in."""
        assert _state(idle_for=timedelta(minutes=6), closing_sent_at=NOW) == (
            SESSION_CLOSING
        )

    def test_closed_outranks_closing(self) -> None:
        """A closed row still carries closing_sent_at; order matters."""
        assert _state(status=STATUS_CLOSED, closing_sent_at=NOW) == SESSION_CLOSED

    def test_closed_outranks_human_ownership(self) -> None:
        assert _state(status=STATUS_CLOSED, mode=MODE_HUMAN) == SESSION_CLOSED


class TestReturningCustomerWindows:
    """Which session a returning customer lands in, by how long they were gone.

    Asserts the configured policy, not the query that applies it -- see the
    module docstring.
    """

    def test_ten_minutes_resumes_the_same_session(self) -> None:
        settings = _settings()
        assert timedelta(minutes=10) <= settings.conversation_reopen_window

    def test_thirty_minutes_is_inside_the_window_at_the_boundary(self) -> None:
        """The default window is exactly thirty minutes, so this is the edge
        case that decides whether the customer is greeted again."""
        settings = _settings()
        assert settings.conversation_reopen_window == timedelta(minutes=30)
        assert timedelta(minutes=30) <= settings.conversation_reopen_window

    def test_three_hours_starts_a_new_session(self) -> None:
        settings = _settings()
        assert timedelta(hours=3) > settings.conversation_reopen_window

    def test_twenty_four_hours_is_a_new_session_by_both_rules(self) -> None:
        settings = _settings()
        assert timedelta(hours=24) > settings.conversation_reopen_window
        assert timedelta(hours=24) >= settings.new_session_after

    def test_new_session_threshold_cannot_undercut_the_reopen_window(self) -> None:
        """Misconfiguring these two the wrong way round would be silent: the
        reopen window would revive a session that the new-session rule had
        already decided was a fresh visit. The property clamps it."""
        settings = _settings(
            conversation_reopen_window_minutes=120,
            new_session_after_hours=1,
        )
        assert settings.new_session_after == settings.conversation_reopen_window

    def test_zero_window_disables_resuming(self) -> None:
        assert _settings(
            conversation_reopen_window_minutes=0
        ).conversation_reopen_window == timedelta(0)


class TestReviveContract:
    """Both reopen paths share one dict, and its contents are load-bearing."""

    def test_reactivates_the_row(self) -> None:
        assert _REVIVE["status"] == STATUS_ACTIVE

    def test_clears_closed_at(self) -> None:
        assert _REVIVE["closed_at"] is None

    def test_clears_closing_sent_at_to_rearm_the_goodbye(self) -> None:
        """claim_idle_sessions only ever considers rows where this is null, so
        leaving it set would make the revived session uncloseable forever."""
        assert _REVIVE["closing_sent_at"] is None

    def test_does_not_touch_welcome_sent_at(self) -> None:
        """The absence is the mechanism, not an oversight: because the welcome
        flag survives, a revived session cannot greet the customer a second
        time and no caller has to remember to suppress it."""
        assert "welcome_sent_at" not in _REVIVE

    def test_touches_nothing_else(self) -> None:
        """Pins the whole contract. Anything added here silently changes what
        reopening means on both paths at once, including history and tags."""
        assert set(_REVIVE) == {"status", "closed_at", "closing_sent_at"}


class TestLifecycleEvents:
    """Payloads the dashboard and the Flutter app both decode by key name."""

    def test_closed_event_carries_every_field_clients_need(self) -> None:
        closed_at = NOW
        event = conversation_closed(
            conversation_id=7,
            user_id=3,
            status=STATUS_CLOSED,
            closed_at=closed_at,
            updated_at=closed_at,
        )
        assert event["type"] == CLOSED == "conversation.closed"
        assert event["conversation_id"] == 7
        assert event["user_id"] == 3
        assert event["status"] == STATUS_CLOSED
        assert event["closed_at"] == closed_at.isoformat()
        assert event["updated_at"] == closed_at.isoformat()

    def test_closed_event_tolerates_null_timestamps(self) -> None:
        """The columns are nullable, and a client receiving null learns
        something true. Formatting must not raise on the way to the bus."""
        event = conversation_closed(
            conversation_id=1,
            user_id=1,
            status=STATUS_CLOSED,
            closed_at=None,
            updated_at=None,
        )
        assert event["closed_at"] is None
        assert event["updated_at"] is None

    def test_reopened_event_distinguishes_who_revived_it(self) -> None:
        """A row that turns active again with no explanation reads as a bug,
        and 'the customer is back' calls for a different reaction than 'a
        colleague just claimed this'."""
        by_customer = conversation_reopened(
            conversation_id=7,
            user_id=3,
            status=STATUS_ACTIVE,
            reason="customer",
            updated_at=NOW,
        )
        by_operator = conversation_reopened(
            conversation_id=7,
            user_id=3,
            status=STATUS_ACTIVE,
            reason="operator",
            updated_at=NOW,
        )
        assert by_customer["type"] == REOPENED == "conversation.reopened"
        assert by_customer["reason"] == "customer"
        assert by_operator["reason"] == "operator"
        assert by_customer["status"] == STATUS_ACTIVE

    def test_events_carry_no_customer_content(self) -> None:
        """The bus is a hint channel. Phone numbers, names and message bodies
        stay behind the authenticated admin API."""
        event = conversation_closed(
            conversation_id=7,
            user_id=3,
            status=STATUS_CLOSED,
            closed_at=NOW,
            updated_at=NOW,
        )
        assert set(event) == {
            "type",
            "conversation_id",
            "user_id",
            "status",
            "closed_at",
            "updated_at",
            "at",
        }


class TestSupersededContract:
    def test_code_is_the_string_both_clients_match_on(self) -> None:
        """The dashboard and the Flutter repository each key off this exact
        string to disable Reply and Take Over. Renaming it would silently turn
        a handled refusal back into an unexplained failure in both."""
        assert ConversationSupersededError.code == "conversation_superseded"
