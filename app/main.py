"""FastAPI application factory and entrypoint."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from app.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.ratelimit import limiter
from app.middleware.metrics import MetricsMiddleware
from app.middleware.request_logging import RequestLoggingMiddleware
from app.routers import admin, events, health, metrics, webhook

settings = get_settings()
configure_logging(debug=settings.debug)
logger = get_logger(__name__)

# Built dashboard assets (produced by `npm run build` in dashboard/).
DASHBOARD_DIST = Path(__file__).resolve().parent.parent / "dashboard" / "dist"

# Vite's dev server, for running the dashboard with hot reload against a
# locally running API.
DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


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


class SpaStaticFiles(StaticFiles):
    """Static files with an index.html fallback for client-side routes.

    Without this, reloading the browser on /dashboard/customers would 404:
    that path exists only in the React router, not on disk.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


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
    if settings.debug:
        # Only in debug: production serves the dashboard same-origin, so no
        # cross-origin access is needed or wanted.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=DEV_ORIGINS,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(RequestLoggingMiddleware)
    if settings.metrics_enabled:
        app.add_middleware(MetricsMiddleware)
        app.include_router(metrics.router)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(webhook.router)
    app.include_router(admin.router)
    app.include_router(events.router)

    if DASHBOARD_DIST.is_dir():
        app.mount(
            "/dashboard",
            SpaStaticFiles(directory=DASHBOARD_DIST, html=True),
            name="dashboard",
        )
    else:
        logger.info("dashboard_not_built", path=str(DASHBOARD_DIST))
    return app


app = create_app()
