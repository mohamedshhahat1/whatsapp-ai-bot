"""Application exceptions and centralized FastAPI exception handlers."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError

from app.core.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    """Base class for expected application errors."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str = "Internal server error") -> None:
        self.message = message
        super().__init__(message)


class NotFoundError(AppError):
    """Requested resource does not exist."""

    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    """The request cannot be satisfied in the resource's current state.

    Distinct from a 400: the request itself is well formed, but the state of
    the conversation, the customer, or a concurrent writer makes it
    impossible right now. Clients can surface these messages directly.
    """

    status_code = 409
    code = "conflict"


class ExternalServiceError(AppError):
    """An upstream service (WhatsApp, OpenAI) failed."""

    status_code = 502
    code = "external_service_error"


class ServiceUnavailableError(AppError):
    """A dependency this request needs is down right now.

    Distinct from ExternalServiceError, which means a third party we called
    misbehaved. This one means our own infrastructure is unreachable and the
    caller should retry rather than change anything about the request.
    """

    status_code = 503
    code = "service_unavailable"


def register_exception_handlers(app: FastAPI) -> None:
    """Attach handlers that convert exceptions into consistent JSON errors."""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "app_error", code=exc.code, message=exc.message, path=request.url.path
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(OperationalError)
    @app.exception_handler(InterfaceError)
    async def handle_database_unavailable(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """A database outage is 503, not 500.

        These two SQLAlchemy errors are the connection-level ones -- refused,
        dropped mid-query, pool exhausted. They previously fell through to the
        catch-all below and came back as 500, which tells a caller the request
        itself was wrong and should not be repeated. The opposite is true: the
        request was fine and retrying is exactly the right response. It also
        misleads whoever is reading the dashboard during an incident.

        Starlette matches handlers along the exception's MRO, so registering
        the specific classes here takes precedence over the ``Exception``
        handler without disturbing it.

        The log records the exception TYPE, deliberately not ``str(exc)``: the
        message for a connection failure carries the database host, port and
        user, and this line is written on every request for as long as the
        outage lasts.
        """
        logger.error(
            "database_unavailable",
            error=type(exc).__name__,
            path=request.url.path,
        )
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "service_unavailable",
                    "message": "Service temporarily unavailable",
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled_error", error=str(exc), path=request.url.path, exc_info=True
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {"code": "internal_error", "message": "Internal server error"}
            },
        )
