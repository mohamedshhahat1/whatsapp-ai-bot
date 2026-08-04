"""Mobile device registration for push notifications.

A separate router from ``admin.py`` rather than two more handlers in it, but
mounted under the same ``/admin`` prefix and behind the same guard, so it is
one API surface with one auth story.

On the path: the specification asked for ``/api/mobile/device-token``. There is
no ``/api`` prefix anywhere in this application and no separate mobile surface
-- the Flutter app already talks to ``/admin`` for conversations, analytics and
replies, and its Dio client is configured with that base. A second prefix would
have meant a second convention for no gain.
"""

from fastapi import APIRouter, Depends, Request, Response, status

from app.core.ratelimit import ADMIN_LIMIT, limiter
from app.dependencies.deps import get_device_service, require_admin
from app.schemas.device_token import (
    DeviceTokenDelete,
    DeviceTokenRead,
    DeviceTokenRegister,
)
from app.services.device_service import DeviceService

router = APIRouter(
    prefix="/admin",
    tags=["devices"],
    dependencies=[Depends(require_admin)],
)


@router.post("/device-token", response_model=DeviceTokenRead)
@limiter.limit(ADMIN_LIMIT)
async def register_device_token(
    request: Request,
    payload: DeviceTokenRegister,
    service: DeviceService = Depends(get_device_service),
) -> DeviceTokenRead:
    """Register this device, or refresh a registration we already have.

    ``request`` is required by slowapi, which inspects the handler signature
    for a parameter of that exact name. A rate-limited route without one
    raises TypeError inside the limiter on every call -- that is not
    hypothetical, it took the webhook down.

    Returns 200 rather than 201 because this is an upsert: the common case by
    far is a device the table already knows, and claiming to have created a
    resource on every app launch would be a lie the client could act on.
    """
    device = await service.register(
        token=payload.token,
        platform=payload.platform,
        notification_privacy=payload.notification_privacy,
    )
    return DeviceTokenRead.model_validate(device)


@router.delete("/device-token", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(ADMIN_LIMIT)
async def delete_device_token(
    request: Request,
    payload: DeviceTokenDelete,
    service: DeviceService = Depends(get_device_service),
) -> Response:
    """Stop notifying this device.

    204 whether or not a row changed. The device asked not to be notified and
    it will not be; reporting 404 for an unknown token would turn this into an
    oracle for which tokens are registered.
    """
    await service.disable(payload.token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
