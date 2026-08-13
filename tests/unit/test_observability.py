from __future__ import annotations

from fastapi.testclient import TestClient

from testops.api.config import Settings
from testops.api.main import API_VERSION, create_app


def test_metrics_require_configured_bearer_and_use_route_templates() -> None:
    app = create_app(
        Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            metrics_token="metrics-test-token",
        )
    )
    with TestClient(app) as client:
        denied = client.get("/metrics")
        assert denied.status_code == 401
        assert denied.headers["www-authenticate"] == "Bearer"

        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        metrics = client.get(
            "/metrics",
            headers={"Authorization": "Bearer metrics-test-token"},
        )

    assert metrics.status_code == 200
    assert metrics.headers["cache-control"] == "no-store"
    assert metrics.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert f'testops_build_info{{version="{API_VERSION}"}} 1.0' in metrics.text
    assert (
        'testops_http_requests_total{method="GET",route="/healthz",status="200"} 2.0'
        in metrics.text
    )
    assert "testops_database_ready 1.0" in metrics.text
    assert "sqlite" not in metrics.text


def test_metrics_are_open_when_no_token_is_configured() -> None:
    app = create_app(Settings(database_url="sqlite+aiosqlite:///:memory:"))
    with TestClient(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert "testops_http_request_duration_seconds" in response.text
