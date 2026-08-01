"""The endpoints the dashboard calls must stay reachable and shaped as typed.

Every admin route is behind the same key, so a single missing dependency or
renamed field breaks a whole page silently at runtime.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

READ_ENDPOINTS = (
    "/admin/stats",
    "/admin/conversations?limit=5",
    "/admin/analytics/daily?days=7",
    "/admin/analytics/models?days=7",
    "/admin/analytics/questions?days=7&limit=5",
    "/admin/analytics/customers?limit=5",
    "/admin/pricing",
    "/admin/knowledge",
)


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_read_endpoints_require_the_admin_key(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_read_endpoints_answer_with_the_admin_key(
    client: TestClient,
    admin_headers: dict[str, str],
    requires_database: None,
    path: str,
) -> None:
    response = client.get(path, headers=admin_headers)
    assert response.status_code == 200


def test_search_requires_a_meaningful_query(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    """A one-character search would scan every message for nothing."""
    response = client.get("/admin/search?q=a", headers=admin_headers)
    assert response.status_code == 422


def test_pricing_can_be_added_listed_and_deleted(
    client: TestClient, admin_headers: dict[str, str], requires_database: None
) -> None:
    model = "test-model-" + uuid4().hex[:8]
    created = client.post(
        "/admin/pricing",
        headers=admin_headers,
        json={
            "model": model,
            "input_price_per_1m": 0.5,
            "output_price_per_1m": 2.0,
            "effective_from": "2026-01-01T00:00:00Z",
            "note": "regression test",
        },
    )
    assert created.status_code == 201
    pricing_id = created.json()["id"]

    try:
        listed = client.get("/admin/pricing", headers=admin_headers)
        assert listed.status_code == 200
        assert any(row["id"] == pricing_id for row in listed.json())

        duplicate = client.post(
            "/admin/pricing",
            headers=admin_headers,
            json={
                "model": model,
                "input_price_per_1m": 0.9,
                "output_price_per_1m": 3.0,
                "effective_from": "2026-01-01T00:00:00Z",
            },
        )
        assert duplicate.status_code == 409
    finally:
        deleted = client.delete(f"/admin/pricing/{pricing_id}", headers=admin_headers)
        assert deleted.status_code == 204


def test_readiness_reports_its_dependencies(
    client: TestClient, requires_database: None
) -> None:
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] is True
    assert body["checks"]["redis"] is True


def test_liveness_does_not_depend_on_anything(client: TestClient) -> None:
    """Liveness must stay up even when a dependency is down."""
    assert client.get("/health").status_code == 200
