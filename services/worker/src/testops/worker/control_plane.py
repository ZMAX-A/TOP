"""Authenticated callback client used by isolated Runner tasks."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx

from testops.contracts import RunResult, RunStatus


class ControlPlaneClient:
    def __init__(self, base_url: str, runner_token: str, *, timeout_seconds: float = 30):
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-Runner-Token": runner_token}
        self._timeout = timeout_seconds

    def report_status(self, run_id: UUID, status: RunStatus) -> dict[str, Any]:
        response = httpx.post(
            f"{self._base_url}/api/v1/internal/runs/{run_id}/status",
            headers=self._headers,
            json={"status": status.value},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json()

    def report_result(self, result: RunResult) -> None:
        response = httpx.post(
            f"{self._base_url}/api/v1/internal/runs/{result.run_id}/result",
            headers=self._headers,
            json=result.model_dump(mode="json", exclude_none=True),
            timeout=self._timeout,
        )
        response.raise_for_status()

    def report_event(self, run_id: UUID, event: dict[str, object]) -> None:
        response = httpx.post(
            f"{self._base_url}/api/v1/internal/runs/{run_id}/events",
            headers=self._headers,
            json=event,
            timeout=self._timeout,
        )
        response.raise_for_status()

    def run_state(self, run_id: UUID) -> dict[str, Any]:
        response = httpx.get(
            f"{self._base_url}/api/v1/internal/runs/{run_id}",
            headers=self._headers,
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json()

    def cancel_requested(self, run_id: UUID) -> bool:
        payload = self.run_state(run_id)
        return bool(payload["cancel_requested"] or payload["status"] == RunStatus.CANCELED.value)

    def report_worker_heartbeat(
        self,
        worker_key: str,
        *,
        pool_key: str,
        display_name: str,
        runner_version: str,
        max_slots: int,
        capabilities: dict[str, object],
    ) -> dict[str, Any]:
        response = httpx.put(
            f"{self._base_url}/api/v1/internal/runner-workers/{worker_key}/heartbeat",
            headers=self._headers,
            json={
                "pool_key": pool_key,
                "display_name": display_name,
                "runner_version": runner_version,
                "max_slots": max_slots,
                "capabilities": capabilities,
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json()
