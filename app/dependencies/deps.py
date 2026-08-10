"""Dependency injection wiring for routers."""

import hmac
from dataclasses import dataclass
from functools import lru_cache
from ipaddress import ip_address

from fastapi import Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.tenant_context import (
    SOURCE_MEMBERSHIP,
    SOURCE_SELECTOR,
    SOURCE_SOLE_TENANT,
    TenantContext,
    system_tenant_context,
)
from app.db.session import get_db
from app.integrations.fcm import FcmClient
from app.integrations.openai import OpenAIClient
from app.integrations.whatsapp import WhatsAppClient
from app.models.operator import LEGACY_OPERATOR_USERNAME
from app.repositories.tenant import (
    count_tenants,
    default_tenant_id,
    sole_tenant_id,
    tenant_exists,
    tenant_ids_for_operator,
)
from app.services.admin_service import AdminService
from app.services.analytics_service import AnalyticsService
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService
from app.services.chat_service import ChatService
from app.services.device_service import DeviceService
from app.services.notification_service import NotificationService
from app.services.pricing_service import PricingService
from app.services.reply_service import ReplyService

BEARER_PREFIX = "Bearer "

# The header a client names a tenant with. A query parameter is accepted too:
# browsers cannot set headers on a WebSocket upgrade, and a tenant id is not a
# secret, so keeping both spellings costs nothing and saves a second
# convention later.
TENANT_HEADER = "X-Tenant-Id"


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


async def get_chat_service(db: AsyncSession = Depends(get_db)) -> ChatService:
    """Chat service bound to the request-scoped database session.

    No route depends on this today: inbound deliveries reach ``ChatService``
    through ``app/services/webhook_processor.py``, which resolves its own
    context. It is kept because it is the wiring a request-scoped inbound
    route would use, and it resolves the tenant the same way that path does
    and under the same decision (D-5). That lookup is a row rather than a
    constant, which is why this is now async.

    Deliberately NOT ``require_tenant_context``. That dependency answers "who
    is this caller, and which tenant may they act inside", and an inbound
    customer message carries no operator credential to answer it with.
    """
    tenant = system_tenant_context(await default_tenant_id(db))
    return ChatService(
        db, get_whatsapp_client(), get_openai_client(), get_settings(), tenant
    )


def get_analytics_service(db: AsyncSession = Depends(get_db)) -> AnalyticsService:
    """Cost and usage analytics bound to the request-scoped session."""
    return AnalyticsService(db, get_settings())


def get_audit_service(db: AsyncSession = Depends(get_db)) -> AuditService:
    """Audit recorder bound to the request-scoped session.

    The same session the acting service uses, so a caller that wants the
    action and its audit row in one transaction can have that by passing
    ``commit=False``.
    """
    return AuditService(db)


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

    Authentication only. Which tenant the caller may act inside is a separate
    question with a separate dependency, because the answer needs the database
    and most of the reasons to refuse are not 401s.
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


def _bad_selector(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _tenant_selector_required() -> HTTPException:
    """Refuse a tenant-scoped request that did not say which tenant.

    400 rather than a guess. Every alternative to this error is a silent
    choice made on the caller's behalf, and the one this codebase would have
    made -- the default tenant -- is the specific outcome D-6 forbids.
    """
    return _bad_selector(
        "This deployment holds more than one tenant. Name the one you are "
        f"acting for with the {TENANT_HEADER} header or the tenant_id query "
        "parameter."
    )


def _no_tenant_access() -> HTTPException:
    """Refuse a credential that can reach no tenant at all.

    403 rather than 404: this says nothing about which tenants exist, only
    that this caller has no way into any of them.
    """
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="This credential is not a member of any tenant",
    )


def _unknown_tenant() -> HTTPException:
    """Refuse a selector naming a tenant this caller may not act inside.

    404 and not 403, and the wording is the same whether the tenant is absent
    or merely someone else's. A 403 here would confirm the tenant exists,
    which turns the selector into an oracle for enumerating tenant ids -- and
    an id worth enumerating is an id worth aiming at the next endpoint.
    """
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found"
    )


def _parse_selector(raw: str, name: str) -> int | None:
    """Read one tenant selector, or None when it was not supplied."""
    value = raw.strip()
    if not value:
        return None
    try:
        selected = int(value)
    except ValueError:
        raise _bad_selector(f"{name} must be an integer tenant id") from None
    if selected <= 0:
        raise _bad_selector(f"{name} must be a positive tenant id")
    return selected


def _selected_tenant_id(x_tenant_id: str, tenant_id: str) -> int | None:
    """The tenant the caller named, from either spelling.

    Two spellings that disagree is a 400 rather than a precedence rule. A
    client sending both with different values has a bug, and picking a winner
    would decide an authorisation boundary by coin toss.
    """
    header = _parse_selector(x_tenant_id, TENANT_HEADER)
    query = _parse_selector(tenant_id, "tenant_id")
    if header is not None and query is not None and header != query:
        raise _bad_selector(
            f"{TENANT_HEADER} and tenant_id name different tenants; send one"
        )
    return header if header is not None else query


async def _platform_tenant_context(
    db: AsyncSession, selected: int | None
) -> TenantContext:
    """Resolve a tenant for the shared ADMIN_API_KEY.

    The key belongs to a deployment, not a person, and holds no membership by
    design -- see app/models/tenant.py. So there is nothing to derive a tenant
    from, and the rules are about what can be known rather than about what was
    granted:

    * exactly one tenant exists -- that is not a preference, it is the only
      tenant there is, and binding to it keeps the existing mobile client
      working without a default-tenant lookup anywhere in the path;
    * more than one -- the caller must say which, and it is validated;
    * none -- nothing to act on, so 403.

    The distinction from a fallback is not cosmetic. ``DEFAULT_TENANT_SLUG``
    would still resolve on a deployment with fifty tenants; this stops
    answering as soon as there are two.
    """
    if selected is not None:
        if not await tenant_exists(db, selected):
            raise _unknown_tenant()
        return TenantContext(selected, SOURCE_SELECTOR, via_legacy_key=True)
    sole = await sole_tenant_id(db)
    if sole is not None:
        return TenantContext(sole, SOURCE_SOLE_TENANT, via_legacy_key=True)
    if await count_tenants(db) == 0:
        raise _no_tenant_access()
    raise _tenant_selector_required()


async def _operator_tenant_context(
    db: AsyncSession, operator_id: int, selected: int | None
) -> TenantContext:
    """Resolve a tenant for an operator authenticated by bearer session.

    Membership is the only source. An operator in one tenant needs to say
    nothing; an operator in several must choose; an operator in none is
    refused rather than shown the deployment's default.

    A selector naming a tenant they are not in answers 404 by way of
    :func:`_unknown_tenant`, so membership cannot be probed from outside.
    """
    reachable = await tenant_ids_for_operator(db, operator_id)
    if not reachable:
        raise _no_tenant_access()
    if selected is not None:
        if selected not in reachable:
            raise _unknown_tenant()
        return TenantContext(selected, SOURCE_SELECTOR)
    if len(reachable) == 1:
        return TenantContext(reachable[0], SOURCE_MEMBERSHIP)
    raise _tenant_selector_required()


async def require_tenant_context(
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    x_tenant_id: str = Header(default="", alias=TENANT_HEADER),
    tenant_id: str = Query(default=""),
) -> TenantContext:
    """The tenant this request is authorised to act inside.

    Declared by every route that touches tenant-owned data. Authentication is
    reused from :func:`require_admin` -- FastAPI caches it, so asking for both
    costs one evaluation -- and this adds the second question: of the tenants
    that exist, which one may this caller act in, and did they say so
    unambiguously.

    Every path out of here either returns a real tenant id or raises. There is
    no branch that resolves the default tenant, and no representation of "all
    tenants": a request that cannot be pinned to exactly one tenant is refused
    rather than widened.
    """
    selected = _selected_tenant_id(x_tenant_id, tenant_id)
    if principal.via_legacy_key:
        return await _platform_tenant_context(db, selected)
    if principal.operator_id is None:
        # Unreachable through require_admin today: only the legacy key yields
        # a principal without an operator. Kept because "no operator and not
        # the legacy key" must never fall through to a tenant, and a future
        # credential type should have to think about this rather than inherit
        # whichever branch happened to be last.
        raise _no_tenant_access()
    return await _operator_tenant_context(db, principal.operator_id, selected)


# Declared after require_tenant_context rather than beside the other service
# factories: Depends() is evaluated when the function is defined, so a factory
# cannot name a dependency that appears later in the module.
def get_admin_service(
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_tenant_context),
) -> AdminService:
    """Admin service bound to the request-scoped session and its tenant.

    Asking for the context here rather than in each route is what makes it
    impossible to add an admin endpoint that reads tenant-owned data without
    one: there is no way to obtain the service without the dependency having
    run. Every reason it can refuse -- no tenant, an ambiguous deployment, a
    selector naming somebody else's tenant -- is decided in one place.

    This binds the request to a tenant; it does not yet check that the
    identifiers in the path belong to it. Those ownership checks are step 4.
    """
    return AdminService(db, tenant)


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

    No tenant context. Metrics are deployment-wide by decision (D-10) and
    carry no tenant label, so there is nothing here to scope.
    """
    if "x-forwarded-for" not in request.headers and _peer_is_internal(request):
        return
    if not _api_key_matches(x_api_key, settings):
        raise _unauthorized("Invalid API key")
