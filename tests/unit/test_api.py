from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from testops.api.main import app


class ApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_health(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_capability_registry_is_exposed(self) -> None:
        response = self.client.get("/api/v1/contracts/capabilities")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        operation_keys = {item["key"] for item in payload["operations"]}
        assertion_keys = {item["key"] for item in payload["assertions"]}
        self.assertIn("retry_report", operation_keys)
        self.assertIn("element_disabled", assertion_keys)

    def test_schema_endpoint_fails_closed(self) -> None:
        self.assertEqual(
            self.client.get("/api/v1/contracts/schemas/run-snapshot").status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/api/v1/contracts/schemas/unknown").status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
