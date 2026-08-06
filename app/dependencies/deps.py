"""Dependency injection wiring for routers."""

import hmac
from dataclasses import dataclass
from functools import lru_cache
from ipaddress import ip_address

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.session import get_db
from app.integrations.fcm import FcmClient
from app.integrations.openai import OpenAIClient
from app.integrations.whatsapp import WhatsAppClient
from app.models.operator import LEGACY_OPERATOR_USERNAME
from app.services.admin_service import AdminService
from app.services.analytics_service import AnalyticsService
from app.services.auth_service import AuthService
from app.services.chat_service import ChatService
from app.services.device_service import DeviceService
from app.services.notification_service import NotificationService
from app.services.pricing_service import PricingService
from app.services.reply_service import ReplyService

BEARER_PREFIX = "Bearer "


@dataclass(frozen=True)
class Principal:
    """Who is making an admin request.

    ``operator_id`` is None for the shared ADMIN_API_KEY. That is not the same
    as "unauthenticated" -- the request is authorised -- it means the caller
    presented a credential that belongs to a deployment rather than a person.
    Anything that records who acted resolves the reserved legacy operator at
    that point, which keeps the shared-key request path free of queries while
    still leaving every stored row attributable.
    """

    operator_id: int | None
    username: str
    is_admin: bool
    via_legacy_key: bool


@lru_cache
def get_whatsapp_client() -> WhatsAppClient:
    """Singleton WhatsApp Cloud API client (shared connection pool)."""
    return WhatsAppClient(get_settings())


@lru_cache
def get_openai_client() -> OpenAIClient:
    """Singleton OpenAI Responses API client."""
    return OpenAIClient(get_settings())


@lru_cache
def get_fcm_client() -> FcmClient:
    """Singleton Firebase client.

    Cached for the same reason as the other two -- a shared connection pool --
    and additionally because it caches the OAuth access token it mints. A
    per-request client would exchange the service-account key for a new token
    on every notification.
    """
    return FcmClient()


def get_chat_service(db: AsyncSession = Depends(get_db)) -> ChatService:
    """Chat service bound to the request-scoped database session."""
    return ChatService(db, get_whatsapp_client(), get_openai_client(), get_settings())


def get_admin_service(db: AsyncSession = Depends(get_db)) -> AdminService:
    """Admin service bound to the request-scoped database session."""
    return AdminService(db)


def get_analytics_service(db: AsyncSession = Depends(get_db)) -> AnalyticsService:
    """Cost and usage analytics bound to the request-scoped session."""
    return AnalyticsService(db, get_settings())


def get_auth_service(db: AsyncSession = Depends(get_db)) -> AuthService:
    """Operator authentication bound to the request-scoped session."""
    return AuthService(db)


def get_pricing_service(db: AsyncSession = Depends(get_db)) -> PricingService:
    """Model pricing history bound to the request-scoped session."""
    return PricingService(db)


def get_reply_service(db: AsyncSession = Depends(get_db)) -> ReplyService:
    """Manual reply service bound to the request-scoped session."""
    return ReplyService(db, get_whatsapp_client())


def get_device_service(db: AsyncSession = Depends(get_db)) -> DeviceService:
    """Device registration bound to the request-scoped session."""
    return DeviceService(db)


def get_notification_service(
    db: AsyncSession = Depends(get_db),
) -> NotificationService:
    """Push notification fan-out bound to the request-scoped session.

    The Firebase client is injected from the process-wide singleton rather
    than constructed per request, so the cached OAuth token survives.
    """
    return NotificationService(db, get_fcm_client())


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _api_key_matches(x_api_key: str, settings: Settings) -> bool:
    """Constant-time check of the shared admin key.

    Extracted so that ``require_metrics_access`` can reuse it. It used to call
    ``require_admin`` directly, which is no longer possible now that the
    latter is async and resolves sessions.
    """
    return bool(x_api_key) and hmac.compare_digest(x_api_key, settings.admin_api_key)


async def require_admin(
    authorization: str = Header(default=""),
    x_api_key: str = Header(default=""),
    settings: Settings = Depends(get_settings),
    auth: AuthService = Depends(get_auth_service),
) -> Principal:
    """Authenticate an admin request and report who made it.

    Returns a :class:`Principal` rather than None, which is what lets an
    endpoint record the actor. Used as a bare ``dependencies=[...]`` guard the
    return value is simply discarded, so every route that does not care is
    unaffected, and FastAPI caches the result per request so declaring it both
    ways costs one evaluation.

    A bearer session is preferred over the shared key when both are present.
    Clients migrating to operator accounts will send both for a while -- the
    mobile app stores the key and has no login screen yet -- and during that
    window the request should be attributed to the person, not the
    deployment.

    The shared-key branch issues no query. ``get_db`` yields a session, but
    SQLAlchemy does not open a connection until something executes, so a
    request carrying only ADMIN_API_KEY still reaches the route without
    touching the database, exactly as before.
    """
    if authorization.startswith(BEARER_PREFIX):
        operator = await auth.resolve(authorization.removeprefix(BEARER_PREFIX).strip())
        if operator is None:
            raise _unauthorized("Invalid or expired session")
        return Principal(
            operator_id=operator.id,
            username=operator.username,
            is_admin=operator.is_admin,
            via_legacy_key=False,
        )
    if _api_key_matches(x_api_key, settings):
        return Principal(
            operator_id=None,
            username=LEGACY_OPERATOR_USERNAME,
            is_admin=True,
            via_legacy_key=True,
        )
    # Message unchanged from before operator accounts existed: clients and
    # tests match on it.
    raise _unauthorized("Invalid API key")


def _peer_is_internal(request: Request) -> bool:
    """True when the socket peer is on a private or loopback address."""
    host = request.client.host if request.client else ""
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def require_metrics_access(
    request: Request,
    x_api_key: str = Header(default=""),
    settings: Settings = Depends(get_settings),
) -> None:
    """Guard for ``GET /metrics``.

    Prometheus scrapes the container directly over the private compose
    network, so a request with no ``X-Forwarded-For`` header from a private
    peer is in-cluster and needs no credential -- Prometheus cannot send a
    custom header in its scrape config anyway.

    Anything that arrived through the reverse proxy carries that header and is
    by definition externally originated, so it must present the admin API key.
    Still one admin credential rather than a second one; this checks the key
    directly now only because ``require_admin`` became async. Operator bearer
    sessions are deliberately NOT accepted here -- this endpoint is for
    machines, and nginx additionally refuses /metrics outright, so this is the
    inner of two layers.
    """
    if "x-forwarded-for" not in request.headers and _peer_is_internal(request):
        return
    if not _api_key_matches(x_api_key, settings):
        raise _unauthorized("Invalid API key")
