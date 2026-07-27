"""FastAPI application factory and entrypoint."""

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.ratelimit import limiter
from app.middleware.request_logging import RequestLoggingMiddleware
from app.routers import admin, health, webhook

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
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(RequestLoggingMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(webhook.router)
    app.include_router(admin.router)
    return app


app = create_app()
