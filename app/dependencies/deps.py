"""Dependency injection wiring for routers."""

import hmac
from functools import lru_cache
from ipaddress import ip_address

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.session import get_db
from app.integrations.openai import OpenAIClient
from app.integrations.whatsapp import WhatsAppClient
from app.services.admin_service import AdminService
from app.services.analytics_service import AnalyticsService
from app.services.chat_service import ChatService
from app.services.pricing_service import PricingService
from app.services.reply_service import ReplyService


@lru_cache
def get_whatsapp_client() -> WhatsAppClient:
    """Singleton WhatsApp Cloud API client (shared connection pool)."""
    return WhatsAppClient(get_settings())


@lru_cache
def get_openai_client() -> OpenAIClient:
    """Singleton OpenAI Responses API client."""
    return OpenAIClient(get_settings())


def get_chat_service(db: AsyncSession = Depends(get_db)) -> ChatService:
    """Chat service bound to the request-scoped database session."""
    return ChatService(db, get_whatsapp_client(), get_openai_client(), get_settings())


def get_admin_service(db: AsyncSession = Depends(get_db)) -> AdminService:
    """Admin service bound to the request-scoped database session."""
    return AdminService(db)


def get_analytics_service(db: AsyncSession = Depends(get_db)) -> AnalyticsService:
    """Cost and usage analytics bound to the request-scoped session."""
    return AnalyticsService(db, get_settings())


def get_pricing_service(db: AsyncSession = Depends(get_db)) -> PricingService:
    """Model pricing history bound to the request-scoped session."""
    return PricingService(db)


def get_reply_service(db: AsyncSession = Depends(get_db)) -> ReplyService:
    """Manual reply service bound to the request-scoped session."""
    return ReplyService(db, get_whatsapp_client())


def require_admin(
    x_api_key: str = Header(default=""),
    settings: Settings = Depends(get_settings),
) -> None:
    """Guard for admin endpoints via the ``X-API-Key`` header."""
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.admin_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key"
        )


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
    Reusing ``require_admin`` keeps one admin credential rather than inventing
    a second one. nginx additionally refuses /metrics outright, so this is the
    inner of two layers.
    """
    if "x-forwarded-for" not in request.headers and _peer_is_internal(request):
        return
    require_admin(x_api_key=x_api_key, settings=settings)
