"""Prometheus metric definitions (shared by the API and the Celery worker).

The API exposes them at ``GET /metrics``; the worker starts its own metrics
HTTP server (see ``app/workers/celery_app.py``) since message processing —
and therefore most OpenAI/message metrics — happens in the worker process.
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

OPENAI_COST_USD_TOTAL = Counter(
    "openai_cost_usd_total",
    "Estimated OpenAI spend in USD (token usage x configured prices)",
    ["model"],
)

ERRORS_TOTAL = Counter(
    "app_errors_total",
    "Application errors by type",
    ["type"],
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
