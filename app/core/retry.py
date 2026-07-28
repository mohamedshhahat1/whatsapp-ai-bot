"""Shared tenacity retry policies for outbound calls (Meta Graph API, OpenAI).

Only *transient* failures are retried (network errors, timeouts, 429, 5xx).
Permanent failures such as 400/401/403 fail immediately — retrying those
would only waste time and hide configuration bugs.

These fast in-process retries are the inner safety net; the Celery task-level
retries (with longer backoff) remain the outer one.
"""

import httpx
import openai
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _log_before_sleep(retry_state: RetryCallState) -> None:
    """Structured log entry before each retry sleep."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    logger.warning(
        "retrying_external_call",
        callee=retry_state.fn.__qualname__ if retry_state.fn else "unknown",
        attempt=retry_state.attempt_number,
        wait_seconds=(
            round(retry_state.next_action.sleep, 2) if retry_state.next_action else None
        ),
        error=str(exc),
    )


def is_transient_http_error(exc: BaseException) -> bool:
    """True for httpx failures worth retrying: network issues, 429, 5xx."""
    if isinstance(exc, httpx.TransportError):  # timeouts, DNS, connection resets
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return False


def is_transient_openai_error(exc: BaseException) -> bool:
    """True for OpenAI SDK failures worth retrying."""
    return isinstance(
        exc,
        (
            openai.APIConnectionError,  # includes APITimeoutError
            openai.RateLimitError,
            openai.InternalServerError,
        ),
    )


def http_retry():
    """Retry decorator for httpx-based clients (Meta Graph API)."""
    settings = get_settings()
    return retry(
        reraise=True,
        retry=retry_if_exception(is_transient_http_error),
        stop=stop_after_attempt(settings.retry_max_attempts),
        wait=wait_exponential_jitter(
            initial=0.5, max=settings.retry_backoff_max_seconds
        ),
        before_sleep=_log_before_sleep,
    )


def openai_retry():
    """Retry decorator for OpenAI SDK calls."""
    settings = get_settings()
    return retry(
        reraise=True,
        retry=retry_if_exception(is_transient_openai_error),
        stop=stop_after_attempt(settings.retry_max_attempts),
        wait=wait_exponential_jitter(
            initial=0.5, max=settings.retry_backoff_max_seconds
        ),
        before_sleep=_log_before_sleep,
    )
