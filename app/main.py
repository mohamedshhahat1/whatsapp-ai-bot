"""FastAPI application factory and entrypoint."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, Request, Response
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.ratelimit import limiter
from app.middleware.metrics import MetricsMiddleware
from app.middleware.request_logging import RequestLoggingMiddleware
from app.routers import admin, health, metrics, webhook

settings = get_settings()
configure_logging(debug=settings.debug)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup/shutdown lifecycle: log boot and dispose the engine cleanly."""
    logger.info("startup", environment=settings.environment)
    yield
    from app.db.session import engine

    await engine.dispose()
    logger.info("shutdown")


async def handle_rate_limit_exceeded(request: Request, exc: Exception) -> Response:
    """Adapt slowapi's handler to Starlette's exception handler signature.

    Starlette types handlers as accepting a bare ``Exception``; slowapi's
    handler is narrowed to ``RateLimitExceeded``. This thin wrapper bridges
    the two without silencing the type checker globally.
    """
    return _rate_limit_exceeded_handler(request, cast(RateLimitExceeded, exc))


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    app = FastAPI(
        title="WhatsApp AI Bot",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, handle_rate_limit_exceeded)
    app.add_middleware(RequestLoggingMiddleware)
    if settings.metrics_enabled:
        app.add_middleware(MetricsMiddleware)
        app.include_router(metrics.router)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(webhook.router)
    app.include_router(admin.router)
    return app


app = create_app()
