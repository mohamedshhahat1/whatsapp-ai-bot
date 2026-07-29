"""C3 regression: Prometheus metrics must not be world-readable.

/metrics exposed message volumes, error counts and latency histograms to
anyone who asked. It is now open only to in-cluster scrapes; anything that
arrived through the proxy must present the admin key.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


@pytest.fixture(autouse=True)
def _skip_when_metrics_disabled() -> None:
    if not get_settings().metrics_enabled:
        pytest.skip("METRICS_ENABLED is false; the router is not mounted")


def test_metrics_requires_a_credential_from_outside(client: TestClient) -> None:
    """The test client's peer is not a private IP, so it must authenticate."""
    response = client.get("/metrics")
    assert response.status_code == 401


def test_metrics_rejects_a_wrong_key(client: TestClient) -> None:
    response = client.get("/metrics", headers={"X-API-Key": "not-the-key"})
    assert response.status_code == 401


def test_proxied_request_must_authenticate_even_from_a_private_peer(
    client: TestClient,
) -> None:
    """An X-Forwarded-For header means the request came from outside."""
    response = client.get("/metrics", headers={"X-Forwarded-For": "10.0.0.9"})
    assert response.status_code == 401


def test_metrics_served_with_the_admin_key(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.get("/metrics", headers=admin_headers)
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


def test_cost_metric_is_not_reintroduced(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    """Spend has one source of truth: the model_pricing table.

    A Prometheus counter computed from the Settings fallback prices used to
    disagree with the dashboard. If it ever comes back, so does the
    disagreement.
    """
    body = client.get("/metrics", headers=admin_headers).text
    assert "openai_cost_usd_total" not in body
