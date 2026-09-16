from __future__ import annotations

from fastapi.testclient import TestClient

from testops.api.config import Settings
from testops.api.main import create_app


def test_authenticated_user_manages_own_run_email_notifications() -> None:
    app = create_app(
        Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            auto_create_schema=True,
            bootstrap_admin_token="bootstrap-test-token",
            smtp_host="smtp.example.com",
            smtp_from_address="testops@example.com",
        )
    )
    with TestClient(app) as client:
        bootstrapped = client.post(
            "/api/v1/auth/bootstrap",
            headers={"X-Bootstrap-Token": "bootstrap-test-token"},
            json={
                "username": "email-admin",
                "display_name": "Email Admin",
                "password": "email-admin-password",
            },
        )
        assert bootstrapped.status_code == 201, bootstrapped.text
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "email-admin", "password": "email-admin-password"},
        )
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        initial = client.get("/api/v1/auth/me/run-email-notifications", headers=headers)
        assert initial.status_code == 200, initial.text
        assert initial.json()["enabled"] is False
        assert initial.json()["smtp_configured"] is True

        saved = client.put(
            "/api/v1/auth/me/run-email-notifications",
            headers=headers,
            json={
                "emails": ["Admin@Example.COM", "Backup@Example.COM"],
                "selected_emails": ["Backup@Example.COM"],
                "enabled": True,
                "statuses": ["PASSED", "FAILED", "INFRA_ERROR"],
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["email"] == "Admin@example.com"
        assert saved.json()["emails"] == ["Admin@example.com", "Backup@example.com"]
        assert saved.json()["selected_emails"] == ["Backup@example.com"]
        assert saved.json()["statuses"] == ["PASSED", "FAILED", "INFRA_ERROR"]

        queued = client.post(
            "/api/v1/auth/me/run-email-notifications/test",
            headers=headers,
        )
        assert queued.status_code == 202, queued.text
        assert queued.json()["destination_display"] == "B***@example.com"
        assert "Admin@example.com" not in queued.text

        restored = client.get("/api/v1/auth/me/run-email-notifications", headers=headers)
        assert restored.status_code == 200
        assert restored.json()["emails"] == ["Admin@example.com", "Backup@example.com"]
        assert restored.json()["selected_emails"] == ["Backup@example.com"]
        assert restored.json()["last_delivery"]["event_type"] == "run.email.test"
        assert restored.json()["last_delivery"]["destination_display"] == "B***@example.com"
