"""Prometheus metric definitions (shared by the API and the Celery worker).

The API exposes them at ``GET /metrics``; the worker starts its own metrics
HTTP server (see ``app/workers/celery_app.py``) since message processing --
and therefore most OpenAI/message metrics -- happens in the worker process.

On cost: there is deliberately no *billing* metric here. Spend is a function
of tokens and the price in force at the time, which lives in the model_pricing
table; the dashboard derives it there. A counter fed from Settings prices
would be a second, silently diverging answer to the same question.

``DAILY_SPEND_USD`` below is not that number and must not be read as it. It is
the spend *guard's* own running total (see ``app/core/quota.py``): approximate,
computed from Settings prices, reset daily, and used only to decide whether the
model is allowed to run. It is exported because a circuit breaker whose
position nobody can see will trip unannounced.
"""

from prometheus_client import Counter, Gauge, Histogram

MESSAGES_TOTAL = Counter(
    "whatsapp_messages_total",
    "WhatsApp messages processed",
    ["direction", "type"],
)

OPENAI_REQUESTS_TOTAL = Counter(
    "openai_requests_total",
    "OpenAI API requests by outcome",
    ["model", "status"],
)

OPENAI_RESPONSE_SECONDS = Histogram(
    "openai_response_seconds",
    "OpenAI response latency in seconds",
    ["model"],
    buckets=(0.25, 0.5, 1, 2, 3, 5, 8, 13, 21, 34),
)

OPENAI_TOKENS_TOTAL = Counter(
    "openai_tokens_total",
    "OpenAI tokens consumed",
    ["model", "kind"],  # kind: prompt | completion
)

ERRORS_TOTAL = Counter(
    "app_errors_total",
    "Application errors by type",
    ["type"],
)

WEBHOOK_DEAD_LETTERS_TOTAL = Counter(
    "webhook_dead_letters_total",
    "Webhook deliveries parked in the dead letter queue after exhausting retries",
)

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "HTTP requests",
    ["method", "path", "status"],
)

HTTP_REQUEST_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
)

# --- Per-customer quotas and abuse protection (app/core/quota.py) -----------

CUSTOMER_RATE_LIMITED_TOTAL = Counter(
    "customer_rate_limited_total",
    "Inbound messages not answered because the customer exceeded a rate window",
    ["window"],  # minute | hour | day
)

CUSTOMER_ABUSE_BLOCKS_TOTAL = Counter(
    "customer_abuse_blocks_total",
    "Customers temporarily blocked by flood or spam detection",
    ["reason"],  # flooding | spamming
)

# --- Spend circuit breaker ---------------------------------------------------

DAILY_SPEND_USD = Gauge(
    "openai_spend_guard_usd_today",
    (
        "Approximate OpenAI spend today as measured by the spend guard. "
        "Uses Settings fallback prices, NOT the model_pricing table -- this is "
        "the circuit breaker's input, not the billing figure."
    ),
)

SPEND_GUARD_TRIPS_TOTAL = Counter(
    "openai_spend_guard_trips_total",
    "Times the daily cost ceiling stopped the model from being called",
    ["kind"],  # usd | tokens
)

AI_DISABLED = Gauge(
    "ai_disabled",
    (
        "1 when the assistant is not answering: either the spend ceiling was "
        "reached or an operator pulled the kill switch. Alert on this."
    ),
)

# The configured ceilings, exported so alert rules can compare against the
# real limit instead of restating it.
#
# A rule that hard-codes "20" keeps measuring against 20 after someone changes
# DAILY_SPEND_LIMIT_USD to 100 -- it then fires permanently, gets muted, and
# the cost ceiling effectively loses its alarm. Exporting the limit makes the
# rule follow the setting.
SPEND_LIMIT_USD = Gauge(
    "openai_spend_guard_limit_usd",
    "Configured daily spend ceiling in USD (DAILY_SPEND_LIMIT_USD).",
)

DAILY_TOKEN_LIMIT = Gauge(
    "openai_daily_token_limit",
    "Configured daily token ceiling (DAILY_TOKEN_LIMIT).",
)

# Explicit zero at import time. An unset gauge is absent from /metrics
# entirely, and an alert on a missing series behaves differently from one on a
# series reading 0 -- the difference between "healthy" and "not scraped yet"
# should not be a guess.
AI_DISABLED.set(0)
DAILY_SPEND_USD.set(0)


def publish_limits() -> None:
    """Export the configured cost ceilings as gauges.

    Imported lazily so this module never takes part in an import cycle with
    ``app.config``, which reaches back into ``app.core`` for its secret
    sources.
    """
    from app.config import get_settings

    settings = get_settings()
    SPEND_LIMIT_USD.set(settings.daily_spend_limit_usd)
    DAILY_TOKEN_LIMIT.set(settings.daily_token_limit)


publish_limits()

# --- Reliability -------------------------------------------------------------

DUPLICATE_DELIVERIES_TOTAL = Counter(
    "webhook_duplicate_deliveries_total",
    (
        "Redeliveries stopped by the idempotency guards, by the stage that "
        "caught them. A rising 'reply_reserved' count means workers are dying "
        "mid-send."
    ),
    ["stage"],  # inbound_claim | reply_reserved | generation_cache
)

# --- Mobile push notifications (app/services/notification_service.py) --------

PUSH_SENT_TOTAL = Counter(
    "push_sent_total",
    (
        "Notifications ACCEPTED by Firebase, by platform and event type. "
        "Accepted is not delivered: FCM queues for offline devices and issues "
        "no receipt, so this counts handoffs to Google, not phones that buzzed."
    ),
    ["platform", "type"],
)

PUSH_FAILED_TOTAL = Counter(
    "push_failed_total",
    (
        "Notifications that could not be handed to Firebase, by reason. "
        "'transient' has already exhausted its retries; 'not_configured' means "
        "push is switched on without usable credentials."
    ),
    ["platform", "reason"],  # transient | rejected | not_configured | unknown
)

PUSH_INVALID_TOKEN_TOTAL = Counter(
    "push_invalid_token_total",
    (
        "Device tokens retired because Firebase said they are permanently "
        "undeliverable (uninstalled app, rotated token, wrong project). A "
        "steady trickle is normal; a spike means a bad credential or a bad "
        "release."
    ),
    ["platform"],
)

PUSH_DELIVERY_LATENCY = Histogram(
    "push_delivery_latency_seconds",
    (
        "Round-trip time of the FCM send call in seconds. Named 'delivery' to "
        "match the spec, but it measures the API round trip only -- Firebase "
        "provides no delivery confirmation, so true time-to-lock-screen is not "
        "observable from here. Do not build a delivery SLO on this."
    ),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13),
)

REGISTERED_DEVICES = Gauge(
    "registered_devices_total",
    (
        "Devices currently enabled to receive push notifications. Reading 0 "
        "means nobody can be notified of anything, which is otherwise "
        "indistinguishable from a quiet day."
    ),
)

# Same reasoning as AI_DISABLED above: an absent series and a series reading
# zero mean different things, and "nothing has registered yet" should not look
# like "not scraped yet".
REGISTERED_DEVICES.set(0)
