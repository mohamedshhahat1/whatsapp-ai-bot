"""HTTP metrics middleware: request counts, latency, and 5xx errors."""

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.metrics import ERRORS_TOTAL, HTTP_REQUEST_SECONDS, HTTP_REQUESTS_TOTAL


def _route_template(request: Request) -> str:
    """Use the route template (e.g. /admin/conversations/{conversation_id})
    instead of the raw path to keep metric cardinality bounded."""
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path)


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record Prometheus metrics for every HTTP request."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            ERRORS_TOTAL.labels(type="unhandled_exception").inc()
            HTTP_REQUESTS_TOTAL.labels(
                method=request.method, path=_route_template(request), status="500"
            ).inc()
            raise
        path = _route_template(request)
        elapsed = time.perf_counter() - started
        HTTP_REQUESTS_TOTAL.labels(
            method=request.method, path=path, status=str(response.status_code)
        ).inc()
        HTTP_REQUEST_SECONDS.labels(method=request.method, path=path).observe(elapsed)
        if response.status_code >= 500:
            ERRORS_TOTAL.labels(type="http_5xx").inc()
        return response
