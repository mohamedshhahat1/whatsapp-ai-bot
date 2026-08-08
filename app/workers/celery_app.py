"""Celery application configured for durable, at-least-once processing.

The guarantee this aims for is: a customer message that reaches the broker is
eventually processed exactly once, even if workers are killed at the worst
possible moment. At-least-once comes from the broker settings here;
exactly-once comes from the idempotency in ChatService, because no broker can
provide it for work that has external side effects.
"""

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_ready, worker_shutdown

from app.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

settings = get_settings()

# How often to look for conversations that have gone idle.
#
# This is the RESOLUTION of the idle timeout, not the timeout itself: with a
# five-minute timeout and a sixty-second sweep, a closing message lands
# between 5:00 and 6:00 after the last activity. That slack is deliberate --
# a customer cannot perceive the difference, and the alternative is either a
# per-session scheduled job (see app/services/session_service.py for why not)
# or sweeping every few seconds for a deadline nobody is watching.
#
# Not derived from conversation_idle_timeout_minutes on purpose. A very short
# timeout should not turn into a very frequent query against every active
# conversation.
SWEEP_INTERVAL_SECONDS = 60

# How often to delete operator sessions whose expiry has passed.
#
# Hourly rather than by the minute, because this is about the size of the
# table and not about security: an expired session is already refused at
# authentication time by OperatorSession.is_valid, so nothing is gained by
# noticing it sixty seconds sooner. Against a twelve-hour session TTL, a row
# survives at most an hour past the point it stopped being usable.
OPERATOR_SESSION_PURGE_INTERVAL_SECONDS = 3600

# How often to expire audit history past AUDIT_RETENTION_DAYS.
#
# Daily, because the horizon it enforces is measured in days. Sweeping more
# often would move rows out a few hours earlier at the cost of a delete
# against the table every admin action writes to. The sweep is batched and
# capped, so a first run facing years of history simply finishes over several
# days rather than in one long transaction.
AUDIT_PURGE_INTERVAL_SECONDS = 86400

# When to roll up yesterday's analytics.
#
# The only entry in the schedule expressed as a time of day rather than an
# interval, and the only one that should be. The others describe a resolution
# ("look for idle sessions every minute"), where the phase does not matter. A
# nightly rollup is different: an 86400-second interval would fire at whatever
# time the beat process last restarted, so "nightly" would drift to the middle
# of the afternoon after one redeploy.
#
# Twenty past midnight UTC, not on the hour, so rows written either side of
# the boundary have settled before the day they belong to is summarised.
ANALYTICS_ROLLUP_HOUR = 0
ANALYTICS_ROLLUP_MINUTE = 20

# Six hours, comfortably inside the daily period. Without an expiry, a stack
# that was down for three days would run three identical ticks on recovery,
# each recomputing the same two days.
ANALYTICS_ROLLUP_EXPIRES_SECONDS = 21600

celery_app = Celery(
    "whatsapp_ai_bot",
    broker=settings.broker_url,
    backend=settings.result_backend,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Durability: ack only after the task finishes, so a worker crash
    # mid-processing re-queues the message instead of losing it.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_default_queue="webhooks",
    result_expires=3600,
    # --- Redelivery window ------------------------------------------------
    # Redis is not AMQP: there is no broker-side ack, so kombu emulates one.
    # A delivery that is not completed within visibility_timeout is handed to
    # another worker -- while the first one is still running it.
    #
    # With task_acks_late that window is the entire duration of the task, and
    # the kombu default is one hour. Setting it explicitly, and well above the
    # hard time limit below, makes the overlap unreachable: a task is killed at
    # 300s, so it can never still be running when the 900s redelivery fires.
    #
    # This must stay greater than task_time_limit. Inverting them means every
    # slow message is processed twice and answered twice.
    broker_transport_options={
        "visibility_timeout": settings.celery_visibility_timeout,
    },
    # Applies the same window to retries scheduled with countdown/ETA, which
    # otherwise use their own default and can be redelivered while pending.
    result_backend_transport_options={
        "visibility_timeout": settings.celery_visibility_timeout,
    },
    # --- Time limits --------------------------------------------------------
    # Without these a wedged socket holds a worker thread forever. Eight of
    # them and the worker is alive, answering its healthcheck, and processing
    # nothing -- the failure mode that looks healthy on every dashboard.
    #
    # The soft limit raises SoftTimeLimitExceeded inside the task so cleanup
    # runs and the delivery can be retried properly. The hard limit kills the
    # thread outright; the gap between them is the cleanup budget.
    task_soft_time_limit=settings.celery_task_soft_time_limit,
    task_time_limit=settings.celery_task_time_limit,
    # --- Scheduled work -----------------------------------------------------
    # Requires a beat process: `celery -A app.workers.celery_app.celery_app
    # beat`. Without one, sessions are still opened, greeted and tracked, but
    # nothing ever closes them and no closing message is sent. See the `beat`
    # service in docker-compose.yml.
    #
    # Exactly ONE beat process may run. Two schedulers means two ticks, and
    # while the claim in ConversationRepository.claim_idle_sessions makes that
    # harmless for correctness, it doubles the query load for nothing.
    #
    # `expires` is the important option here. Beat keeps emitting ticks while
    # the workers are down, and without an expiry they queue up: bring the
    # workers back after an hour and sixty identical sweeps run at once, each
    # scanning the same table. Expiring a tick after one interval means a
    # recovering worker runs the newest sweep and discards the backlog, which
    # is exactly right -- a sweep is a statement about now, and a stale one has
    # nothing to say.
    beat_schedule={
        "close-idle-conversation-sessions": {
            "task": "conversations.close_idle_sessions",
            "schedule": float(SWEEP_INTERVAL_SECONDS),
            "options": {
                "queue": "webhooks",
                "expires": float(SWEEP_INTERVAL_SECONDS),
            },
        },
        "purge-expired-operator-sessions": {
            "task": "operators.purge_expired_sessions",
            "schedule": float(OPERATOR_SESSION_PURGE_INTERVAL_SECONDS),
            "options": {
                "queue": "webhooks",
                "expires": float(OPERATOR_SESSION_PURGE_INTERVAL_SECONDS),
            },
        },
        "purge-expired-audit-logs": {
            "task": "audit.purge_expired_logs",
            "schedule": float(AUDIT_PURGE_INTERVAL_SECONDS),
            "options": {
                "queue": "webhooks",
                "expires": float(AUDIT_PURGE_INTERVAL_SECONDS),
            },
        },
        "rollup-daily-analytics": {
            "task": "analytics.rollup_daily",
            "schedule": crontab(
                hour=ANALYTICS_ROLLUP_HOUR,
                minute=ANALYTICS_ROLLUP_MINUTE,
            ),
            "options": {
                "queue": "webhooks",
                "expires": float(ANALYTICS_ROLLUP_EXPIRES_SECONDS),
            },
        },
    },
    # --- Shutdown -----------------------------------------------------------
    # On SIGTERM, stop taking new work and let in-flight tasks finish. This is
    # what makes a deploy safe: with acks_late, a task killed mid-flight is
    # redelivered, and redelivery after a WhatsApp send is how one customer
    # message becomes two replies. The reservation in ChatService catches that
    # if it happens; finishing cleanly means it does not have to.
    #
    # The compose stop_grace_period must exceed task_time_limit, or Docker
    # SIGKILLs before Celery can drain and the protection is theatre.
    worker_cancel_long_running_tasks_on_connection_loss=False,
    # Emit task events so `celery events` and Flower can see what a worker was
    # doing when it died. Cheap, and the alternative during an incident is
    # guessing.
    worker_send_task_events=True,
    task_send_sent_event=True,
)


@worker_ready.connect
def _start_metrics_server(**_kwargs: object) -> None:
    """Expose worker metrics for Prometheus.

    Requires the threads pool (single process) so all task metrics live in
    this process: run the worker with ``--pool=threads``.
    """
    if settings.metrics_enabled:
        from prometheus_client import start_http_server

        start_http_server(settings.worker_metrics_port)


@worker_shutdown.connect
def _log_shutdown(**_kwargs: object) -> None:
    """Mark the end of a clean drain.

    Its absence from the logs is the signal worth having: a worker that stopped
    without this line was killed rather than drained, which means whatever it
    was processing will be redelivered. During a deploy that distinguishes a
    healthy rollout from one that answered some customers twice.
    """
    logger.info("worker_shutdown_complete")
