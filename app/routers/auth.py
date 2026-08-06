"""Operator authentication endpoints.

Mounted at /admin/auth but NOT on the admin router, because that one carries
router-level ``Depends(require_admin)`` and a login endpoint that requires
you to already be logged in is of limited use.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.core.ratelimit import ADMIN_LIMIT, limiter
from app.dependencies.deps import Principal, get_auth_service, require_admin
from app.schemas.operator import LoginRequest, LoginResponse, OperatorRead, WhoAmIRead
from app.services.auth_service import AuthenticationError, AuthService

router = APIRouter(prefix="/admin/auth", tags=["auth"])

BEARER_PREFIX = "Bearer "


@router.post("/login", response_model=LoginResponse)
# Tighter than ADMIN_LIMIT on purpose. Every other admin route is behind a
# credential; this is the one an anonymous caller can reach, so it is the one
# that can be guessed at.
@limiter.limit("10/minute")
async def login(
    request: Request,
    payload: LoginRequest,
    auth: AuthService = Depends(get_auth_service),
) -> LoginResponse:
    """Exchange a username and password for a bearer session.

    The token is returned once and only its SHA-256 is stored, so it cannot
    be recovered from the database afterwards. Send it as
    ``Authorization: Bearer <token>``.
    """
    try:
        operator, token, expires_at = await auth.login(
            payload.username,
            payload.password,
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    return LoginResponse(
        token=token,
        expires_at=expires_at,
        operator=OperatorRead.model_validate(operator),
    )


@router.post("/logout", status_code=204)
@limiter.limit(ADMIN_LIMIT)
async def logout(
    request: Request,
    authorization: str = Header(default=""),
    principal: Principal = Depends(require_admin),
    auth: AuthService = Depends(get_auth_service),
) -> None:
    """End the current session.

    Idempotent, and a no-op for a caller authenticated with the shared
    ADMIN_API_KEY -- there is no session to end, and returning an error for
    a client that asked to be logged out and now is would be pedantry.
    """
    if principal.via_legacy_key:
        return
    if authorization.startswith(BEARER_PREFIX):
        await auth.revoke(authorization.removeprefix(BEARER_PREFIX).strip())


@router.get("/me", response_model=WhoAmIRead)
@limiter.limit(ADMIN_LIMIT)
async def whoami(
    request: Request,
    principal: Principal = Depends(require_admin),
) -> WhoAmIRead:
    """Who the presented credential belongs to."""
    return WhoAmIRead(
        operator_id=principal.operator_id,
        username=principal.username,
        is_admin=principal.is_admin,
        via_legacy_key=principal.via_legacy_key,
    )
