"""Middleware that logs every HTTP request with latency and status code."""

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger

logger = get_logger("request")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Structured request logging with a per-request correlation id."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = uuid.uuid4().hex[:12]
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.error(
                "request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
            raise
        finally:
            structlog.contextvars.clear_contextvars()
        logger.info(
            "request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        response.headers["X-Request-ID"] = request_id
        return response
