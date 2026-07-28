"""Prometheus metric definitions (shared by the API and the Celery worker).

The API exposes them at ``GET /metrics``; the worker starts its own metrics
HTTP server (see ``app/workers/celery_app.py``) since message processing --
and therefore most OpenAI/message metrics -- happens in the worker process.

Note there is no spend metric here. Cost is a function of tokens and the price
in force at the time, which lives in the model_pricing table; the dashboard
derives it there. A counter fed from Settings prices would be a second,
silently diverging answer to the same question.
"""

from prometheus_client import Counter, Histogram

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
