"""Per-customer quotas, abuse detection and the spend circuit breaker.

These cover the protections added for Task 5. The emphasis is split roughly
evenly between "does it block the right things" and "does it fail open",
because every one of these checks sits on the path of every customer message.
A guard that starts refusing paying customers the moment Redis blips is worse
than the abuse it was written to prevent, so that behaviour is tested as
deliberately as the blocking is.
"""

import pytest

from app.config import Settings
from app.core import quota
from tests.fake_redis import Clock, FakeRedis

pytestmark = pytest.mark.asyncio

WA_ID = "201001234567"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def settings() -> Settings:
    """Small, round limits so the tests read as arithmetic rather than trivia."""
    return Settings().model_copy(
        update={
            "customer_rate_limit_enabled": True,
            "spend_guard_enabled": True,
            "customer_limit_per_minute": 3,
            "customer_limit_per_hour": 10,
            "customer_limit_per_day": 20,
            "flood_burst_messages": 5,
            "flood_burst_seconds": 10,
            "duplicate_message_limit": 3,
            "duplicate_message_window_seconds": 300,
            "abuse_block_seconds": 900,
            "daily_spend_limit_usd": 10.0,
            "daily_token_limit": 1_000_000,
            "spend_alert_threshold": 0.8,
        }
    )


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> FakeRedis:
    """Install a fake Redis and align the quota module's clock with it."""
    fake = FakeRedis(clock)

    async def _client(_settings: Settings) -> FakeRedis:
        return fake

    monkeypatch.setattr(quota, "_client", _client)
    monkeypatch.setattr(quota.time, "time", clock)
    return fake


async def _send(settings: Settings, text: str = "hello", n: int = 1):
    """Send n distinct messages, returning the last decision."""
    decision = quota.ALLOWED
    for i in range(n):
        decision = await quota.check(WA_ID, f"wamid.{text}.{i}", text, settings)
    return decision


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


async def test_messages_within_the_limit_are_allowed(redis, settings) -> None:
    for i in range(settings.customer_limit_per_minute):
        decision = await quota.check(WA_ID, f"wamid.{i}", "hi", settings)
        assert decision.allowed, f"message {i + 1} should have been allowed"


async def test_the_message_past_the_minute_limit_is_refused(redis, settings) -> None:
    await _send(settings, n=settings.customer_limit_per_minute)
    decision = await quota.check(WA_ID, "wamid.over", "hi", settings)

    assert not decision.allowed
    assert decision.reason == quota.RATE_LIMITED
    # The first refusal explains itself. Later ones must not, or the refusals
    # become the flood.
    assert decision.notify is True


async def test_only_the_first_refusal_notifies(redis, settings) -> None:
    await _send(settings, n=settings.customer_limit_per_minute)
    first = await quota.check(WA_ID, "wamid.over1", "hi", settings)
    second = await quota.check(WA_ID, "wamid.over2", "hi", settings)
    third = await quota.check(WA_ID, "wamid.over3", "hi", settings)

    assert first.notify is True
    assert second.notify is False
    assert third.notify is False


async def test_the_window_really_slides(redis, settings, clock) -> None:
    """A minute later the customer is served again, without any key expiring.

    This is the difference between a sliding window and a fixed one. With
    fixed buckets a customer refused at 11:59:59 is served at 12:00:00 and
    refused again immediately, which is both unfair and exploitable.
    """
    await _send(settings, n=settings.customer_limit_per_minute)
    assert not (await quota.check(WA_ID, "wamid.over", "hi", settings)).allowed

    clock.advance(61)

    assert (await quota.check(WA_ID, "wamid.later", "hi", settings)).allowed


async def test_a_redelivery_does_not_consume_a_second_slot(redis, settings) -> None:
    """The same wa_message_id counts once, however many times it arrives.

    Without the NX add, a Celery retry storm would rate-limit precisely the
    customer it was struggling to serve -- each retry of an unanswered message
    consuming another slot until they were refused outright.
    """
    for _ in range(10):
        decision = await quota.check(WA_ID, "wamid.same", "hi", settings)

    assert decision.allowed


async def test_limits_are_per_customer(redis, settings) -> None:
    """One customer flooding must never consume another's allowance.

    This is the entire reason this module exists rather than relying on the
    IP-based endpoint limit, where every customer shares Meta's address.
    """
    for i in range(settings.customer_limit_per_minute + 2):
        await quota.check("201000000001", f"wamid.a{i}", "hi", settings)

    other = await quota.check("201000000002", "wamid.b0", "hi", settings)
    assert other.allowed


async def test_hourly_limit_applies_beyond_the_minute_windows(
    redis, settings, clock
) -> None:
    """Spacing messages out to dodge the minute limit still hits the hour one."""
    for i in range(settings.customer_limit_per_hour):
        assert (await quota.check(WA_ID, f"wamid.h{i}", "hi", settings)).allowed
        clock.advance(61)

    decision = await quota.check(WA_ID, "wamid.hover", "hi", settings)
    assert not decision.allowed
    assert decision.reason == quota.RATE_LIMITED


# ---------------------------------------------------------------------------
# Abuse detection
# ---------------------------------------------------------------------------


async def test_a_burst_faster_than_typing_earns_a_block(redis, settings) -> None:
    for i in range(settings.flood_burst_messages + 1):
        decision = await quota.check(WA_ID, f"wamid.f{i}", f"msg {i}", settings)

    assert not decision.allowed
    assert decision.reason == quota.FLOODING
    assert decision.retry_after_seconds == settings.abuse_block_seconds


async def test_a_block_short_circuits_and_stays_quiet(redis, settings) -> None:
    """Once blocked, further messages are dropped silently.

    The customer was told when the block was applied. Repeating it for every
    message of an ongoing flood turns our own refusals into the flood.
    """
    for i in range(settings.flood_burst_messages + 1):
        await quota.check(WA_ID, f"wamid.f{i}", f"msg {i}", settings)

    following = await quota.check(WA_ID, "wamid.after", "still here", settings)
    assert not following.allowed
    assert following.reason == quota.BLOCKED
    assert following.notify is False


async def test_a_block_expires_on_its_own(redis, settings, clock) -> None:
    for i in range(settings.flood_burst_messages + 1):
        await quota.check(WA_ID, f"wamid.f{i}", f"msg {i}", settings)

    clock.advance(settings.abuse_block_seconds + 1)

    assert (await quota.check(WA_ID, "wamid.fresh", "hello again", settings)).allowed


async def test_repeating_the_same_text_is_spam(redis, settings, clock) -> None:
    """Identical text repeated, but slowly enough not to trip flood detection."""
    for i in range(settings.duplicate_message_limit + 1):
        decision = await quota.check(WA_ID, f"wamid.s{i}", "BUY NOW", settings)
        clock.advance(11)  # outside the burst window

    assert not decision.allowed
    assert decision.reason == quota.SPAMMING


async def test_spam_detection_normalises_whitespace_and_case(
    redis, settings, clock
) -> None:
    """"PRICE?" and "price ?" are the same message, which is what a script sends."""
    variants = ["PRICE?", "price?", "  Price ?  ", "PRICE ?", "price   ?"]
    for i, text in enumerate(variants):
        decision = await quota.check(WA_ID, f"wamid.v{i}", text, settings)
        clock.advance(11)

    assert not decision.allowed
    assert decision.reason == quota.SPAMMING


async def test_an_operator_can_lift_a_block(redis, settings) -> None:
    """Flood detection is a heuristic; six photos of a damaged wall trip it."""
    for i in range(settings.flood_burst_messages + 1):
        await quota.check(WA_ID, f"wamid.f{i}", f"msg {i}", settings)

    assert await quota.unblock(WA_ID, settings) is True
    assert (await quota.check(WA_ID, "wamid.next", "sorry about that", settings)).allowed


async def test_unblocking_someone_who_is_not_blocked_is_not_an_error(
    redis, settings
) -> None:
    assert await quota.unblock("201009999999", settings) is False


# ---------------------------------------------------------------------------
# Spend circuit breaker
# ---------------------------------------------------------------------------


async def test_reaching_the_dollar_ceiling_stops_everyone(redis, settings) -> None:
    await quota.record_usage(
        prompt_tokens=30_000_000,
        completion_tokens=0,
        model="gpt-4.1-mini",
        settings=settings,
    )

    decision = await quota.check(WA_ID, "wamid.x", "hi", settings)
    assert not decision.allowed
    assert decision.reason == quota.SPEND_EXCEEDED
    assert decision.blocked_for_cost is True
    # A cost block must still tell the customer something -- silence from a
    # business is worse than "a colleague will call you".
    assert decision.notify is True


async def test_the_kill_switch_stops_the_model(redis, settings) -> None:
    await quota.set_ai_disabled(True, settings)

    decision = await quota.check(WA_ID, "wamid.x", "hi", settings)
    assert not decision.allowed
    assert decision.reason == quota.AI_DISABLED_MANUALLY

    await quota.set_ai_disabled(False, settings)
    assert (await quota.check(WA_ID, "wamid.y", "hi", settings)).allowed


async def test_spend_is_costed_from_settings_prices(redis, settings) -> None:
    await quota.record_usage(
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        model="gpt-4.1-mini",
        settings=settings,
    )

    snapshot = await quota.usage_snapshot(settings)
    expected = settings.openai_input_price_per_1m + settings.openai_output_price_per_1m
    assert snapshot["spend_usd"] == pytest.approx(expected, rel=1e-6)
    assert snapshot["tokens"] == 2_000_000


async def test_the_snapshot_reports_the_limits_and_blocked_count(
    redis, settings
) -> None:
    for i in range(settings.flood_burst_messages + 1):
        await quota.check(WA_ID, f"wamid.f{i}", f"msg {i}", settings)

    snapshot = await quota.usage_snapshot(settings)
    assert snapshot["available"] is True
    assert snapshot["blocked_customers"] == 1
    assert snapshot["limits"]["per_minute"] == settings.customer_limit_per_minute
    assert snapshot["ai_disabled"] is False


# ---------------------------------------------------------------------------
# Failure behaviour -- as important as the blocking behaviour
# ---------------------------------------------------------------------------


async def test_everything_is_allowed_when_redis_is_down(
    monkeypatch, settings, clock
) -> None:
    """Fail open. A cache outage must not stop the business answering customers."""
    async def _broken(_settings):
        raise ConnectionError("redis is gone")

    monkeypatch.setattr(quota, "_client", _broken)

    for i in range(50):
        assert (await quota.check(WA_ID, f"wamid.{i}", "hi", settings)).allowed


async def test_a_failure_midway_through_a_check_still_allows(
    monkeypatch, settings, clock
) -> None:
    from tests.fake_redis import FakeRedis

    fake = FakeRedis(clock, unavailable=True)

    async def _client(_settings):
        return fake

    monkeypatch.setattr(quota, "_client", _client)
    monkeypatch.setattr(quota.time, "time", clock)

    assert (await quota.check(WA_ID, "wamid.1", "hi", settings)).allowed


async def test_recording_usage_never_raises(monkeypatch, settings) -> None:
    """The reply has already been generated and sent. Losing the counter is bad;
    raising here would be worse -- it would fail work that already succeeded."""
    async def _broken(_settings):
        raise ConnectionError("redis is gone")

    monkeypatch.setattr(quota, "_client", _broken)

    await quota.record_usage(
        prompt_tokens=100, completion_tokens=50, model="m", settings=settings
    )


async def test_the_snapshot_says_so_when_it_cannot_tell(monkeypatch, settings) -> None:
    """Never report zero spend when the real answer is unknown.

    Zero renders as a reassuring empty chart at exactly the moment the guard
    has failed open and is protecting nothing.
    """
    async def _broken(_settings):
        raise ConnectionError("redis is gone")

    monkeypatch.setattr(quota, "_client", _broken)

    snapshot = await quota.usage_snapshot(settings)
    assert snapshot["available"] is False
    assert "spend_usd" not in snapshot


async def test_checks_are_skipped_entirely_when_both_are_disabled(
    monkeypatch, settings
) -> None:
    """No Redis round trip at all when the feature is off."""
    async def _explode(_settings):
        raise AssertionError("Redis must not be contacted when quotas are off")

    monkeypatch.setattr(quota, "_client", _explode)
    off = settings.model_copy(
        update={"customer_rate_limit_enabled": False, "spend_guard_enabled": False}
    )

    assert (await quota.check(WA_ID, "wamid.1", "hi", off)).allowed


# ---------------------------------------------------------------------------
# Stress
# ---------------------------------------------------------------------------


async def test_a_sustained_flood_stays_bounded_and_never_raises(
    redis, settings, clock
) -> None:
    """Five hundred messages from one number: every one decided, none crash.

    Also asserts the customer is told at most a handful of times across the
    whole flood. Replying to each one would mean paying WhatsApp to argue with
    a script.
    """
    notified = 0
    allowed = 0
    for i in range(500):
        decision = await quota.check(WA_ID, f"wamid.{i}", f"message {i}", settings)
        notified += int(decision.notify)
        allowed += int(decision.allowed)
        clock.advance(0.5)

    assert allowed <= settings.customer_limit_per_day
    assert notified <= 5, f"replied to the flood {notified} times"


async def test_message_copy_matches_the_reason(settings) -> None:
    cost = quota.QuotaDecision(allowed=False, reason=quota.SPEND_EXCEEDED, notify=True)
    abuse = quota.QuotaDecision(allowed=False, reason=quota.FLOODING, notify=True)
    rate = quota.QuotaDecision(allowed=False, reason=quota.RATE_LIMITED, notify=True)

    assert quota.message_for(cost, "+20100") == quota.capacity_message("+20100")
    assert quota.message_for(abuse) == quota.ABUSE_MESSAGE
    assert quota.message_for(rate) == quota.RATE_LIMIT_MESSAGE


async def test_the_capacity_message_never_mentions_money(settings) -> None:
    """Budget problems are ours, not the customer's.

    It must also never leak a figure -- this copy is sent on a path where the
    model is switched off, so nothing else is guarding it.
    """
    from app.services import price_policy

    message = quota.capacity_message("+201000000000")
    assert not price_policy.mentions_amount(message, "+201000000000")
