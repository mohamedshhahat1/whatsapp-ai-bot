"""Liveness and readiness probes.

The two are deliberately different. Liveness answers "is this process
wedged?" and must not depend on anything external, or a Redis blip restarts
every API container. Readiness answers "can this replica serve traffic right
now?" and must check the dependencies a request actually needs, so an orchestrator
takes the replica out of rotation instead of serving 500s.
"""

import asyncio
from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.config import get_settings
from app.core.logging import get_logger
from app.db.session import SessionLocal

logger = get_logger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the process is up and the event loop is responsive."""
    return {"status": "ok"}


async def _check_database() -> bool:
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("readiness_database_unavailable", error=str(exc))
        return False


async def _check_redis() -> bool:
    settings = get_settings()
    if not settings.rate_limit_enabled and not settings.use_task_queue:
        # Nothing in this configuration talks to Redis.
        return True
    try:
        from redis.asyncio import Redis

        client = Redis.from_url(settings.redis_url)
        try:
            await client.ping()
        finally:
            await client.aclose()
        return True
    except Exception as exc:
        logger.warning("readiness_redis_unavailable", error=str(exc))
        return False


@router.get("/health/ready")
async def ready(response: Response) -> dict[str, Any]:
    """Readiness: the database and queue backend are reachable.

    Both checks run concurrently, so the probe costs one round trip rather
    than two and stays well inside a typical 5s probe timeout.
    """
    database_ok, redis_ok = await asyncio.gather(_check_database(), _check_redis())
    ready_now = database_ok and redis_ok
    if not ready_now:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if ready_now else "not_ready",
        "checks": {"database": database_ok, "redis": redis_ok},
    }
