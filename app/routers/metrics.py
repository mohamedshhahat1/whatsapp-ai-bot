"""Prometheus scrape endpoint for the API process.

Not public: see ``require_metrics_access``. The metrics carry customer volume,
spend and error rates, and the exposition format leaks every route path.
"""

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.dependencies.deps import require_metrics_access

router = APIRouter(tags=["observability"])


@router.get(
    "/metrics",
    include_in_schema=False,
    dependencies=[Depends(require_metrics_access)],
)
async def metrics() -> Response:
    """Expose Prometheus metrics in text exposition format."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
