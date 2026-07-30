"""Per-customer quotas, abuse detection and the OpenAI spend circuit breaker.

Why this exists alongside ``app/core/ratelimit.py``:

slowapi limits the HTTP endpoint by client IP. That is the correct thing for
the admin API, where each operator really is a different address. It does
almost nothing for the webhook, because every delivery arrives from Meta's
infrastructure -- one bucket shared by every customer in the business. One
person holding down send consumes the allowance for everyone, and the abuser
is indistinguishable from the abused.

The identity that matters here is the WhatsApp number, and it is only known
after the payload has been parsed, which is inside the worker rather than at
the edge. So these checks live on the processing path, in front of every paid
call.

Three independent protections:

1. **Rate**   -- sliding minute/hour/day windows per wa_id.
2. **Abuse**  -- flood bursts and repeated identical text, which buy a
                 temporary block rather than a per-message refusal.
3. **Spend**  -- daily USD and token ceilings across all customers, which
                 switch the model off globally when breached.

Everything fails OPEN. If Redis is down, every check allows the message and
logs the failure. A protective measure that silently stops answering paying
customers when a cache is unavailable is worse than the abuse it prevents.
"""

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.core.logging import get_logger
from app.core.metrics import (
    AI_DISABLED,
    CUSTOMER_ABUSE_BLOCKS_TOTAL,
    CUSTOMER_RATE_LIMITED_TOTAL,
    DAILY_SPEND_USD,
    SPEND_GUARD_TRIPS_TOTAL,
)

logger = get_logger(__name__)

# Key namespace. Everything is prefixed so a shared Redis stays legible and
# `redis-cli --scan --pattern 'quota:*'` shows the whole subsystem.
_MESSAGES = "quota:msgs:"      # sorted set, one member per message, 24h of history
_DUPLICATE = "quota:dup:"      # counter per (customer, message body)
_BLOCKED = "quota:blocked:"    # presence = temporarily blocked
_SPEND = "quota:spend:"        # daily USD, float
_TOKENS = "quota:tokens:"      # daily tokens, int
_ALERTED = "quota:alerted:"    # de-duplicates alerts within a day
_KILL_SWITCH = "quota:ai:disabled"  # set by an operator, cleared by an operator

# Reasons, used for metrics labels, logs and choosing the customer-facing copy.
RATE_LIMITED = "rate_limited"
FLOODING = "flooding"
SPAMMING = "spamming"
BLOCKED = "blocked"
SPEND_EXCEEDED = "spend_exceeded"
TOKENS_EXCEEDED = "tokens_exceeded"
AI_DISABLED_MANUALLY = "ai_disabled"


@dataclass(frozen=True)
class QuotaDecision:
    """The outcome of the checks for one inbound message.

    ``notify`` distinguishes "tell the customer something" from "drop this
    silently". A customer who has genuinely tripped a limit gets one
    explanation; the next forty messages of the same flood get nothing, or the
    reply itself becomes the flood.
    """

    allowed: bool
    reason: str | None = None
    notify: bool = False
    retry_after_seconds: int | None = None

    @property
    def blocked_for_cost(self) -> bool:
        return self.reason in (SPEND_EXCEEDED, TOKENS_EXCEEDED, AI_DISABLED_MANUALLY)


ALLOWED = QuotaDecision(allowed=True)


# ---------------------------------------------------------------------------
# Customer-facing copy.
#
# Arabic, matching the persona. These are sent WITHOUT a model call -- the
# whole point is that the customer has hit a limit, so spending a completion
# to phrase the refusal would defeat it.
# ---------------------------------------------------------------------------

RATE_LIMIT_MESSAGE = (
    "\u0648\u0635\u0644\u062a\u0646\u0627 \u0631\u0633\u0627\u0626\u0644\u0643 \u0648\u0634\u0643\u0631\u064b\u0627 \u0644\u062a\u0648\u0627\u0635\u0644\u0643 \u0645\u0639\u0646\u0627. \u062f\u0639\u0646\u0627 \u0646\u0623\u062e\u0630 \u062f\u0642\u064a\u0642\u0629 "
    "\u0644\u0644\u0631\u062f \u0639\u0644\u064a\u0643 \u0628\u0634\u0643\u0644 \u0645\u0646\u0627\u0633\u0628 \u0648\u0643\u0627\u0645\u0644. \u0645\u0646 \u0641\u0636\u0644\u0643 \u0623\u0631\u0633\u0644 \u0633\u0624\u0627\u0644\u0643 "
    "\u0641\u064a \u0631\u0633\u0627\u0644\u0629 \u0648\u0627\u062d\u062f\u0629 \u0648\u0633\u0646\u0648\u0627\u0641\u064a\u0643 \u062d\u0642\u0647 \u0641\u0648\u0631\u064b\u0627."
)

ABUSE_MESSAGE = (
    "\u0644\u0627\u062d\u0638\u0646\u0627 \u0639\u062f\u062f\u064b\u0627 \u0643\u0628\u064a\u0631\u064b\u0627 \u0645\u0646 \u0627\u0644\u0631\u0633\u0627\u0626\u0644 \u0627\u0644\u0645\u062a\u0643\u0631\u0631\u0629. "
    "\u0633\u0646\u062a\u0648\u0642\u0641 \u0645\u0624\u0642\u062a\u064b\u0627 \u0639\u0646 \u0627\u0644\u0631\u062f \u0627\u0644\u0622\u0644\u064a \u0644\u0641\u062a\u0631\u0629 \u0642\u0635\u064a\u0631\u0629. "
    "\u0625\u0630\u0627 \u0643\u0646\u062a \u062a\u062d\u062a\u0627\u062c \u0645\u0633\u0627\u0639\u062f\u0629 \u0639\u0627\u062c\u0644\u0629 \u0641\u0627\u0643\u062a\u0628 \u0643\u0644\u0645\u0629 \u0645\u0648\u0638\u0641 "
    "\u0648\u0633\u064a\u062a\u0648\u0627\u0635\u0644 \u0645\u0639\u0643 \u0623\u062d\u062f \u0632\u0645\u0644\u0627\u0626\u0646\u0627."
)


def capacity_message(sales_phone: str = "") -> str:
    """Copy for when the model is off because a cost ceiling was reached.

    Says nothing about budgets or technical limits -- that is our problem, not
    the customer's. It reads as a handover to a person, because that is what
    it is, and it is the one path that still works when the AI is disabled.
    """
    base = (
        "\u0634\u0643\u0631\u064b\u0627 \u0644\u062a\u0648\u0627\u0635\u0644\u0643 \u0645\u0639\u0646\u0627. \u0627\u0644\u062e\u062f\u0645\u0629 \u0627\u0644\u0622\u0644\u064a\u0629 \u063a\u064a\u0631 \u0645\u062a\u0627\u062d\u0629 "
        "\u062d\u0627\u0644\u064a\u064b\u0627\u060c \u0648\u0633\u064a\u062a\u0648\u0627\u0635\u0644 \u0645\u0639\u0643 \u0623\u062d\u062f \u0632\u0645\u0644\u0627\u0626\u0646\u0627 \u0641\u064a \u0623\u0642\u0631\u0628 \u0648\u0642\u062a."
    )
    if sales_phone:
        return f"{base}\n\n\U0001f4de \u0644\u0644\u062a\u0648\u0627\u0635\u0644 \u0627\u0644\u0645\u0628\u0627\u0634\u0631: {sales_phone}"
    return (
        base
        + "\n\n\u0645\u0646 \u0641\u0636\u0644\u0643 \u0627\u062a\u0631\u0643 \u0631\u0642\u0645 \u0647\u0627\u062a\u0641\u0643 \u0648\u0633\u0646\u062a\u0635\u0644 \u0628\u0643."
    )


def message_for(decision: QuotaDecision, sales_phone: str = "") -> str:
    """The copy a blocked customer should receive, if any."""
    if decision.blocked_for_cost:
        return capacity_message(sales_phone)
    if decision.reason in (FLOODING, SPAMMING, BLOCKED):
        return ABUSE_MESSAGE
    return RATE_LIMIT_MESSAGE


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _fingerprint(text: str | None) -> str:
    """Stable short hash of a message body, for duplicate detection.

    Hashed rather than stored: the key would otherwise contain the customer's
    own words, and Redis is not where customer message content belongs.
    Normalised for whitespace and case so "PRICE?" and "price ?" count as the
    same message, which is what a script sending them looks like.
    """
    normalised = " ".join((text or "").lower().split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


async def _client(settings: Settings) -> Any:
    """One short-lived Redis client per call.

    Same reasoning as ``app/core/events.py``: the Celery worker runs each task
    in its own event loop, and a cached asyncio client would hold connections
    bound to a loop that has already closed. All the work below is pipelined,
    so it is a single round trip regardless.
    """
    from redis.asyncio import Redis

    return Redis.from_url(settings.redis_url, decode_responses=True)


async def check(
    wa_id: str,
    wa_message_id: str,
    text: str | None,
    settings: Settings,
) -> QuotaDecision:
    """Decide whether this inbound message may reach the model.

    Called after the message has been stored and before anything is paid for.
    Storing first is deliberate: a throttled customer's messages must still
    appear in the operator's transcript. We decline to *answer*, we never
    decline to *listen*.

    Order matters. The global spend guard is checked first, because when it is
    tripped nothing should reach the model regardless of who is asking, and
    checking it first means a flood during an outage does not also churn
    per-customer counters.
    """
    if not settings.customer_rate_limit_enabled and not settings.spend_guard_enabled:
        return ALLOWED

    try:
        client = await _client(settings)
    except Exception as exc:
        logger.warning("quota_unavailable", error=str(exc))
        return ALLOWED

    try:
        if settings.spend_guard_enabled:
            spend_decision = await _check_spend(client, settings)
            if not spend_decision.allowed:
                return spend_decision

        if not settings.customer_rate_limit_enabled:
            return ALLOWED

        return await _check_customer(client, wa_id, wa_message_id, text, settings)
    except Exception as exc:
        # Fail open, loudly.
        logger.warning("quota_check_failed", wa_id_hash=_fingerprint(wa_id), error=str(exc))
        return ALLOWED
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


async def _check_spend(client: Any, settings: Settings) -> QuotaDecision:
    """Global cost ceiling. Off means off, for everybody."""
    day = _today()

    async with client.pipeline(transaction=False) as pipe:
        pipe.exists(_KILL_SWITCH)
        pipe.get(f"{_SPEND}{day}")
        pipe.get(f"{_TOKENS}{day}")
        killed, spend_raw, tokens_raw = await pipe.execute()

    if killed:
        AI_DISABLED.set(1)
        return QuotaDecision(
            allowed=False, reason=AI_DISABLED_MANUALLY, notify=True
        )

    spend = float(spend_raw or 0.0)
    tokens = int(tokens_raw or 0)
    DAILY_SPEND_USD.set(spend)

    if spend >= settings.daily_spend_limit_usd:
        AI_DISABLED.set(1)
        SPEND_GUARD_TRIPS_TOTAL.labels(kind="usd").inc()
        await _alert_once(
            client,
            f"spend:{day}",
            "spend_limit_reached",
            spend_usd=round(spend, 4),
            limit_usd=settings.daily_spend_limit_usd,
        )
        return QuotaDecision(allowed=False, reason=SPEND_EXCEEDED, notify=True)

    if tokens >= settings.daily_token_limit:
        AI_DISABLED.set(1)
        SPEND_GUARD_TRIPS_TOTAL.labels(kind="tokens").inc()
        await _alert_once(
            client,
            f"tokens:{day}",
            "token_limit_reached",
            tokens=tokens,
            limit=settings.daily_token_limit,
        )
        return QuotaDecision(allowed=False, reason=TOKENS_EXCEEDED, notify=True)

    AI_DISABLED.set(0)

    # Early warning, once per day per kind, while there is still time to act.
    threshold = settings.spend_alert_threshold
    if spend >= settings.daily_spend_limit_usd * threshold:
        await _alert_once(
            client,
            f"spend-warn:{day}",
            "spend_approaching_limit",
            spend_usd=round(spend, 4),
            limit_usd=settings.daily_spend_limit_usd,
        )
    if tokens >= settings.daily_token_limit * threshold:
        await _alert_once(
            client,
            f"tokens-warn:{day}",
            "tokens_approaching_limit",
            tokens=tokens,
            limit=settings.daily_token_limit,
        )

    return ALLOWED


async def _check_customer(
    client: Any,
    wa_id: str,
    wa_message_id: str,
    text: str | None,
    settings: Settings,
) -> QuotaDecision:
    """Sliding-window rate limits, flood and spam detection for one number."""
    now = time.time()
    messages_key = f"{_MESSAGES}{wa_id}"
    blocked_key = f"{_BLOCKED}{wa_id}"

    # An existing block short-circuits everything. Checked before the counters
    # are touched so a customer in a loop is not still accumulating history.
    ttl = await client.ttl(blocked_key)
    if ttl and ttl > 0:
        return QuotaDecision(
            allowed=False,
            reason=BLOCKED,
            notify=False,  # they were already told when the block was applied
            retry_after_seconds=int(ttl),
        )

    day_ago = now - 86400

    # One sorted set holds 24 hours of this customer's message timestamps, and
    # every window is a ZCOUNT over it. Three separate counters would expire at
    # three different moments and disagree with each other at the boundaries;
    # this way the minute, hour and day windows are all genuinely sliding and
    # all derived from the same facts.
    #
    # NX matters: the member is the wa_message_id, so a Celery retry of a
    # delivery that was already counted does not count it again. Without it, a
    # redelivery storm would rate-limit the very customer it is trying to serve.
    async with client.pipeline(transaction=True) as pipe:
        pipe.zremrangebyscore(messages_key, 0, day_ago)
        pipe.zadd(messages_key, {wa_message_id or f"t{now}": now}, nx=True)
        pipe.zcount(messages_key, now - settings.flood_burst_seconds, now)
        pipe.zcount(messages_key, now - 60, now)
        pipe.zcount(messages_key, now - 3600, now)
        pipe.zcount(messages_key, day_ago, now)
        pipe.expire(messages_key, 86400)
        _, _, burst, per_minute, per_hour, per_day, _ = await pipe.execute()

    # --- Flood: faster than a person can type. Earns a block, not a refusal.
    if burst > settings.flood_burst_messages:
        await client.setex(blocked_key, settings.abuse_block_seconds, FLOODING)
        CUSTOMER_ABUSE_BLOCKS_TOTAL.labels(reason=FLOODING).inc()
        logger.warning(
            "customer_flood_blocked",
            wa_id_hash=_fingerprint(wa_id),
            burst=burst,
            window_seconds=settings.flood_burst_seconds,
            block_seconds=settings.abuse_block_seconds,
        )
        return QuotaDecision(
            allowed=False,
            reason=FLOODING,
            notify=True,
            retry_after_seconds=settings.abuse_block_seconds,
        )

    # --- Spam: the same text over and over.
    if text:
        dup_key = f"{_DUPLICATE}{wa_id}:{_fingerprint(text)}"
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(dup_key)
            pipe.expire(dup_key, settings.duplicate_message_window_seconds)
            repeats, _ = await pipe.execute()

        if repeats > settings.duplicate_message_limit:
            await client.setex(blocked_key, settings.abuse_block_seconds, SPAMMING)
            CUSTOMER_ABUSE_BLOCKS_TOTAL.labels(reason=SPAMMING).inc()
            logger.warning(
                "customer_spam_blocked",
                wa_id_hash=_fingerprint(wa_id),
                repeats=repeats,
                block_seconds=settings.abuse_block_seconds,
            )
            return QuotaDecision(
                allowed=False,
                reason=SPAMMING,
                notify=True,
                retry_after_seconds=settings.abuse_block_seconds,
            )

    # --- Ordinary rate limits. No block: the customer is not misbehaving,
    #     they are simply ahead of what we will answer this minute.
    for count, limit, window, retry_after in (
        (per_minute, settings.customer_limit_per_minute, "minute", 60),
        (per_hour, settings.customer_limit_per_hour, "hour", 3600),
        (per_day, settings.customer_limit_per_day, "day", 86400),
    ):
        if count > limit:
            CUSTOMER_RATE_LIMITED_TOTAL.labels(window=window).inc()
            logger.info(
                "customer_rate_limited",
                wa_id_hash=_fingerprint(wa_id),
                window=window,
                count=count,
                limit=limit,
            )
            # Only the first message past the line gets an explanation. Past
            # that the refusals would themselves become the flood.
            return QuotaDecision(
                allowed=False,
                reason=RATE_LIMITED,
                notify=(count == limit + 1),
                retry_after_seconds=retry_after,
            )

    return ALLOWED


async def record_usage(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    model: str,
    settings: Settings,
) -> None:
    """Add one completion to today's running spend and token totals.

    Costed with the Settings fallback prices rather than the model_pricing
    table, and that is a deliberate divergence worth naming: this number
    exists to trip a breaker within milliseconds on the hot path, not to bill
    anyone. The authoritative figure -- the one the dashboard shows and the
    one that stays correct across historical price changes -- is still derived
    from ai_logs joined to model_pricing.

    An approximate ceiling checked before every call is worth more than an
    exact one computed after the money is gone.

    Never raises. Failing to record usage must not fail a reply that has
    already been generated and sent.
    """
    if not settings.spend_guard_enabled:
        return

    total_tokens = max(0, prompt_tokens) + max(0, completion_tokens)
    if total_tokens == 0:
        return

    cost = (
        prompt_tokens * settings.openai_input_price_per_1m
        + completion_tokens * settings.openai_output_price_per_1m
    ) / 1_000_000

    day = _today()
    try:
        client = await _client(settings)
        try:
            async with client.pipeline(transaction=True) as pipe:
                pipe.incrbyfloat(f"{_SPEND}{day}", cost)
                pipe.incrby(f"{_TOKENS}{day}", total_tokens)
                # Two days, so the admin endpoint can still show yesterday.
                pipe.expire(f"{_SPEND}{day}", 172800)
                pipe.expire(f"{_TOKENS}{day}", 172800)
                await pipe.execute()
        finally:
            await client.aclose()
    except Exception as exc:
        logger.warning("spend_record_failed", model=model, error=str(exc))


async def _alert_once(
    client: Any, marker: str, event: str, **fields: object
) -> None:
    """Emit an alert-worthy log line at most once per day per marker.

    Without the marker, breaching the ceiling logs an alert for every
    subsequent message -- thousands of identical warnings, which is how a real
    alert gets muted. The marker key expires at the end of the day, so a fresh
    breach tomorrow alerts again.
    """
    try:
        if await client.set(f"{_ALERTED}{marker}", "1", ex=86400, nx=True):
            logger.warning(event, **fields)
    except Exception:
        # An alert we could not de-duplicate is still worth emitting.
        logger.warning(event, **fields)


# ---------------------------------------------------------------------------
# Operator-facing helpers, surfaced through the admin API.
# ---------------------------------------------------------------------------


async def usage_snapshot(settings: Settings) -> dict[str, Any]:
    """Today's spend and token totals, plus the state of the breaker."""
    day = _today()
    try:
        client = await _client(settings)
        try:
            async with client.pipeline(transaction=False) as pipe:
                pipe.get(f"{_SPEND}{day}")
                pipe.get(f"{_TOKENS}{day}")
                pipe.exists(_KILL_SWITCH)
                spend_raw, tokens_raw, killed = await pipe.execute()

            blocked = 0
            async for _ in client.scan_iter(match=f"{_BLOCKED}*", count=500):
                blocked += 1
        finally:
            await client.aclose()
    except Exception as exc:
        logger.warning("quota_snapshot_failed", error=str(exc))
        return {"available": False, "error": str(exc)}

    spend = float(spend_raw or 0.0)
    tokens = int(tokens_raw or 0)

    return {
        "available": True,
        "date": day,
        "spend_usd": round(spend, 4),
        "spend_limit_usd": settings.daily_spend_limit_usd,
        "spend_used_fraction": (
            round(spend / settings.daily_spend_limit_usd, 4)
            if settings.daily_spend_limit_usd
            else 0.0
        ),
        "tokens": tokens,
        "token_limit": settings.daily_token_limit,
        "ai_disabled": bool(killed),
        "spend_guard_enabled": settings.spend_guard_enabled,
        "customer_rate_limit_enabled": settings.customer_rate_limit_enabled,
        "blocked_customers": blocked,
        "limits": {
            "per_minute": settings.customer_limit_per_minute,
            "per_hour": settings.customer_limit_per_hour,
            "per_day": settings.customer_limit_per_day,
        },
    }


async def set_ai_disabled(disabled: bool, settings: Settings) -> None:
    """Manual kill switch for the model.

    Separate from the spend ceiling so an operator can stop the bot during an
    incident -- a bad knowledge deploy, a prompt regression -- without editing
    configuration and redeploying. Customers keep being received and stored;
    they are simply routed to a person.
    """
    client = await _client(settings)
    try:
        if disabled:
            await client.set(_KILL_SWITCH, "1")
            AI_DISABLED.set(1)
            logger.warning("ai_disabled_by_operator")
        else:
            await client.delete(_KILL_SWITCH)
            AI_DISABLED.set(0)
            logger.warning("ai_enabled_by_operator")
    finally:
        await client.aclose()


async def unblock(wa_id: str, settings: Settings) -> bool:
    """Lift an abuse block early. Returns True if one was actually lifted.

    Needed because the detector is heuristic. A genuine customer sending six
    photos of a damaged wall in ten seconds looks exactly like a flood, and an
    operator must be able to undo that immediately rather than explaining to
    them that they have to wait fifteen minutes.
    """
    client = await _client(settings)
    try:
        removed = await client.delete(f"{_BLOCKED}{wa_id}")
        if removed:
            logger.info("customer_unblocked", wa_id_hash=_fingerprint(wa_id))
        return bool(removed)
    finally:
        await client.aclose()
