"""Celery tasks.

Each task runs its own event loop (``asyncio.run``), so all async resources
(asyncpg connections, httpx clients) are created inside that loop rather than
shared across tasks -- sharing them across event loops is unsafe. The flip
side is that every one of them must also be closed before the loop ends,
which is what the ``finally`` block below is for.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import redis
from celery import Task
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.channels.config import get_channel_settings
from app.channels.messenger import MessengerAdapter
from app.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.metrics import ERRORS_TOTAL, WEBHOOK_DEAD_LETTERS_TOTAL
from app.core.retention_config import get_retention_settings
from app.integrations.openai import OpenAIClient
from app.integrations.whatsapp import WhatsAppClient
from app.repositories.analytics import PriceDefaults
from app.repositories.analytics_rollup import (
    AnalyticsRollupRepository,
    complete_days_before,
)
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService
from app.services.session_service import SessionService
from app.services.webhook_processor import (
    process_meta_payload,
    process_webhook_payload,
)
from app.workers.celery_app import celery_app

settings = get_settings()
configure_logging(debug=settings.debug)
logger = get_logger(__name__)

MAX_RETRIES = 5

# How many complete days each nightly rollup recomputes.
#
# Two rather than one so a single missed night heals itself: the next run
# covers yesterday and the day before, and the upsert makes the second pass
# over an already-summarised day a no-op in effect. It does not heal a longer
# outage -- see rollup_daily_analytics for why that is left explicit.
ANALYTICS_ROLLUP_LOOKBACK_DAYS = 2


async def _close_all(
    resources: tuple[tuple[str, Callable[[], Awaitable[Any]]], ...],
) -> None:
    """Close each resource in the loop that created it, best-effort.

    A failure here must not mask the original exception, or a retriable error
    would surface as a shutdown error and lose its traceback.
    """
    for label, close in resources:
        try:
            await close()
        except Exception:
            logger.warning("resource_close_failed", resource=label, exc_info=True)


async def _run(payload: dict[str, Any]) -> None:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    whatsapp = WhatsAppClient(settings)
    ai = OpenAIClient(settings)
    try:
        async with session_factory() as session:
            await process_webhook_payload(session, whatsapp, ai, settings, payload)
    finally:
        # Every client opened in this loop is closed in this loop, in reverse
        # order of creation.
        await _close_all(
            (
                ("openai", ai.aclose),
                ("whatsapp", whatsapp.aclose),
                ("engine", engine.dispose),
            )
        )


async def _run_meta(payload: dict[str, Any]) -> None:
    """The Messenger equivalent of :func:`_run`.

    Deliberately a sibling rather than a branch inside ``_run``: the two build
    different clients, and adding a channel argument to the task that carries
    live WhatsApp traffic would change its signature for no benefit.

    The adapter is created here, inside the task's own event loop, for the
    reason in the module docstring -- an httpx client belongs to the loop that
    opened it -- and is closed alongside the others.
    """
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    adapter = MessengerAdapter(get_channel_settings())
    ai = OpenAIClient(settings)
    try:
        async with session_factory() as session:
            await process_meta_payload(session, adapter, ai, settings, payload)
    finally:
        await _close_all(
            (
                ("openai", ai.aclose),
                ("messenger", adapter.aclose),
                ("engine", engine.dispose),
            )
        )


async def _sweep_idle_sessions() -> int:
    """Close every conversation that has gone idle.

    No OpenAI client: a closing message is fixed company copy, never generated.

    Only the WhatsApp client is built here, and that is not an oversight. The
    sweep sends on whichever channel each claimed session came from, but the
    two kinds of transport are acquired differently: WhatsApp reuses this
    shared Cloud API client, while every other channel's adapter builds its
    own from channel settings when the sweep first needs it. SessionService
    closes those in its own ``finally``, inside this same event loop.
    """
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    whatsapp = WhatsAppClient(settings)
    try:
        async with session_factory() as session:
            service = SessionService(session, settings, whatsapp)
            return await service.close_idle_sessions()
    finally:
        await _close_all(
            (
                ("whatsapp", whatsapp.aclose),
                ("engine", engine.dispose),
            )
        )


async def _purge_expired_operator_sessions() -> int:
    """Delete operator sessions whose expiry has passed.

    No clients at all: this touches one table and talks to nothing outside
    the database.
    """
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            return await AuthService(session).purge_expired_sessions()
    finally:
        await _close_all((("engine", engine.dispose),))


async def _purge_audit_logs() -> int:
    """Delete audit rows past the retention horizon.

    Returns zero without opening a connection when retention is disabled,
    which is what AUDIT_RETENTION_DAYS=0 means.
    """
    retention = get_retention_settings()
    if not retention.enforced:
        return 0
    cutoff = datetime.now(UTC) - retention.audit_retention
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            return await AuditService(session).purge_older_than(cutoff)
    finally:
        await _close_all((("engine", engine.dispose),))


def _price_defaults() -> PriceDefaults:
    """Fallback prices for calls no model_pricing row covers.

    Built from the same two settings AnalyticsService uses, so an unpriced
    call costs the same in the rollup as it does in the live query.
    """
    return PriceDefaults(
        input_price=Decimal(str(settings.openai_input_price_per_1m)),
        output_price=Decimal(str(settings.openai_output_price_per_1m)),
    )


async def _rollup_analytics(lookback: int) -> int:
    """Recompute the stored summary for the most recent complete days.

    No external clients: this reads two tables and writes a third.
    """
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            days = complete_days_before(datetime.now(UTC), lookback)
            processed = await AnalyticsRollupRepository(session).rollup_days(
                days, _price_defaults()
            )
            await session.commit()
            return processed
    finally:
        await _close_all((("engine", engine.dispose),))


def _dead_letter(payload: dict[str, Any], error: BaseException) -> None:
    """Park a delivery that exhausted its retries.

    Celery's default behaviour after the last retry is to record a failure in
    the result backend, which expires after an hour -- the customer's message
    is then gone with no trace. Pushing the raw payload onto a capped Redis
    list means it can be inspected and replayed, and the counter gives
    Prometheus something to alert on.
    """
    entry = json.dumps(
        {
            "failed_at": datetime.now(UTC).isoformat(),
            "error": f"{type(error).__name__}: {error}",
            "payload": payload,
        }
    )
    try:
        with redis.Redis.from_url(settings.redis_url) as client:
            client.lpush(settings.dead_letter_key, entry)
            client.ltrim(
                settings.dead_letter_key, 0, settings.dead_letter_max_entries - 1
            )
    except Exception:
        # Losing the dead letter is bad, but raising here would replace the
        # real error with a Redis error in the logs.
        logger.error("dead_letter_write_failed", exc_info=True)


@celery_app.task(
    bind=True,
    name="webhooks.process_event",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=MAX_RETRIES,
    retry_kwargs={"max_retries": MAX_RETRIES},
)
def process_webhook_event(self: Task, payload: dict[str, Any]) -> None:
    """Durably process one webhook delivery with exponential-backoff retries.

    Retrying is safe because processing is idempotent end to end: the inbound
    message is claimed with ON CONFLICT DO NOTHING, the completion is cached
    against the inbound id so a replay is not re-billed, and the outbound row
    is reserved under a unique constraint before the WhatsApp call. A retry
    that lands after a successful send finds all three and does nothing.
    """
    try:
        asyncio.run(_run(payload))
    except SoftTimeLimitExceeded as exc:
        # The task overran its soft limit. This is a retry-worthy condition,
        # not a bug in the payload: usually a hung external call. Counted
        # separately because a rising rate means OpenAI or Meta is degraded,
        # which looks nothing like an application error.
        ERRORS_TOTAL.labels(type="task_soft_timeout").inc()
        logger.error(
            "webhook_task_timeout",
            retries=self.request.retries,
            soft_limit_seconds=settings.celery_task_soft_time_limit,
        )
        if self.request.retries >= MAX_RETRIES:
            WEBHOOK_DEAD_LETTERS_TOTAL.inc()
            _dead_letter(payload, exc)
        raise
    except Exception as exc:
        if self.request.retries >= MAX_RETRIES:
            WEBHOOK_DEAD_LETTERS_TOTAL.inc()
            logger.error(
                "webhook_dead_lettered",
                retries=self.request.retries,
                error=str(exc),
            )
            _dead_letter(payload, exc)
        raise


@celery_app.task(
    bind=True,
    name="webhooks.process_meta_event",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=MAX_RETRIES,
    retry_kwargs={"max_retries": MAX_RETRIES},
)
def process_meta_webhook_event(self: Task, payload: dict[str, Any]) -> None:
    """Durably process one Messenger delivery.

    Idempotent for the same three reasons as the WhatsApp task -- the inbound
    claim, the generation cache and the outbound reservation all key off the
    provider message id -- plus one specific to this channel: the adapter
    discards the page's own echoes before parsing, so a replayed delivery can
    never be mistaken for a customer turn.
    """
    try:
        asyncio.run(_run_meta(payload))
    except SoftTimeLimitExceeded as exc:
        ERRORS_TOTAL.labels(type="task_soft_timeout").inc()
        logger.error(
            "meta_webhook_task_timeout",
            retries=self.request.retries,
            soft_limit_seconds=settings.celery_task_soft_time_limit,
        )
        if self.request.retries >= MAX_RETRIES:
            WEBHOOK_DEAD_LETTERS_TOTAL.inc()
            _dead_letter(payload, exc)
        raise
    except Exception as exc:
        if self.request.retries >= MAX_RETRIES:
            WEBHOOK_DEAD_LETTERS_TOTAL.inc()
            logger.error(
                "meta_webhook_dead_lettered",
                retries=self.request.retries,
                error=str(exc),
            )
            _dead_letter(payload, exc)
        raise


@celery_app.task(
    bind=True,
    name="conversations.close_idle_sessions",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=2,
)
def close_idle_sessions(self: Task) -> None:
    """Periodic sweep: end sessions with no activity for the configured time.

    Emitted by beat every SWEEP_INTERVAL_SECONDS. Safe to run concurrently
    with itself and safe to retry, because every session is taken with a
    conditional UPDATE before anything is sent: a second runner claims a
    disjoint set, and a retry claims only what the failed attempt did not.
    Neither can produce a second goodbye.

    WhatsApp and Messenger conversations are both swept, and each goodbye
    leaves through the adapter for the channel its session came from. The set
    is not simply every channel; see
    ``ConversationRepository.SWEEPABLE_CHANNELS`` for what membership of it
    promises and why a channel that cannot send must stay out of it.

    Retries are few and quick on purpose. A sweep is a statement about the
    present, so a failed one is better replaced by the next scheduled tick
    than retried for several minutes against a world that has moved on.
    """
    try:
        closed = asyncio.run(_sweep_idle_sessions())
    except SoftTimeLimitExceeded:
        ERRORS_TOTAL.labels(type="session_sweep_timeout").inc()
        logger.error("session_sweep_timeout", retries=self.request.retries)
        raise
    except Exception as exc:
        logger.error(
            "session_sweep_failed",
            retries=self.request.retries,
            error=str(exc),
        )
        raise

    if closed:
        logger.info("session_sweep_completed", closed=closed)


@celery_app.task(
    bind=True,
    name="operators.purge_expired_sessions",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=2,
)
def purge_expired_operator_sessions(self: Task) -> None:
    """Periodic sweep: delete operator sessions that have expired.

    Emitted by beat every OPERATOR_SESSION_PURGE_INTERVAL_SECONDS. Safe to
    run concurrently with itself and safe to retry: the delete is bounded by
    a timestamp that only moves forward, so a second runner or a later
    attempt simply finds fewer rows, and a session that is still live can
    never be caught by it.

    Retries are few and quick for the same reason the idle sweep's are -- the
    next tick is a better answer than a long retry against a table that has
    moved on.
    """
    started = time.monotonic()
    try:
        deleted = asyncio.run(_purge_expired_operator_sessions())
    except SoftTimeLimitExceeded:
        ERRORS_TOTAL.labels(type="operator_session_purge_timeout").inc()
        logger.error(
            "operator_session_purge_timeout",
            retries=self.request.retries,
        )
        raise
    except Exception as exc:
        logger.error(
            "operator_session_purge_failed",
            retries=self.request.retries,
            error=str(exc),
        )
        raise

    # Logged unconditionally, the zero case included. A sweep that finds
    # nothing is the expected steady state, and the absence of this line is
    # how you notice beat has stopped emitting the tick at all.
    logger.info(
        "operator_session_purge_completed",
        deleted=deleted,
        duration_seconds=round(time.monotonic() - started, 3),
    )


@celery_app.task(
    bind=True,
    name="audit.purge_expired_logs",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=2,
)
def purge_expired_audit_logs(self: Task) -> None:
    """Periodic sweep: expire audit history past AUDIT_RETENTION_DAYS.

    Emitted by beat every AUDIT_PURGE_INTERVAL_SECONDS. Does nothing at all
    when retention is disabled, which is the default-safe reading of a
    deployment that never set the variable.

    Safe to retry and safe to run twice: the cutoff is recomputed from the
    clock each time, so a second runner finds fewer rows rather than deleting
    anything the first should have kept. Each batch commits on its own, so an
    interrupted sweep keeps the work it already did.
    """
    started = time.monotonic()
    try:
        deleted = asyncio.run(_purge_audit_logs())
    except SoftTimeLimitExceeded:
        ERRORS_TOTAL.labels(type="audit_purge_timeout").inc()
        logger.error("audit_purge_timeout", retries=self.request.retries)
        raise
    except Exception as exc:
        logger.error(
            "audit_purge_failed",
            retries=self.request.retries,
            error=str(exc),
        )
        raise

    logger.info(
        "audit_purge_completed",
        deleted=deleted,
        duration_seconds=round(time.monotonic() - started, 3),
    )


@celery_app.task(
    bind=True,
    name="analytics.rollup_daily",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
)
def rollup_daily_analytics(self: Task, lookback: int | None = None) -> None:
    """Nightly job: store the pre-aggregated summary for completed days.

    Emitted by beat once a day. Safe to run twice and safe to retry: the day
    is the primary key of analytics_daily and the write is an upsert, so a
    repeat recomputes the same figures in place rather than duplicating them.

    Retries are more generous than the other periodic tasks, and the reason is
    the opposite of theirs. The idle sweep gives up quickly because its next
    tick is a minute away and a fresh sweep beats a retried one. Here the next
    tick is twenty-four hours away, so abandoning a failed run leaves a
    missing day on the dashboard until tomorrow.

    ``lookback`` overrides how many complete days are recomputed, for
    backfilling by hand::

        celery -A app.workers.celery_app.celery_app call \\
            analytics.rollup_daily --args='[30]'

    Backfilling is deliberately manual. Beat down for a week leaves a gap the
    normal two-day lookback cannot reach, and having every tick scan an
    unbounded range to find out would be a worse trade than leaving the gap
    visible and filling it deliberately.
    """
    started = time.monotonic()
    days = ANALYTICS_ROLLUP_LOOKBACK_DAYS if lookback is None else lookback
    try:
        processed = asyncio.run(_rollup_analytics(days))
    except SoftTimeLimitExceeded:
        ERRORS_TOTAL.labels(type="analytics_rollup_timeout").inc()
        logger.error("analytics_rollup_timeout", retries=self.request.retries)
        raise
    except Exception as exc:
        logger.error(
            "analytics_rollup_failed",
            retries=self.request.retries,
            error=str(exc),
        )
        raise

    # Logged unconditionally: this runs once a day, so the line is cheap, and
    # its absence is the only signal that the nightly tick stopped arriving.
    logger.info(
        "analytics_rollup_completed",
        days=processed,
        duration_seconds=round(time.monotonic() - started, 3),
    )
