from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from testops.api.artifact_store import ArtifactAccess
from testops.api.config import Settings
from testops.api.main import create_app
from testops.api.persistence import (
    ArtifactRecord,
    AuditLogRecord,
    DispatchOutboxRecord,
    RunCaseRecord,
    RunEventRecord,
)
from testops.contracts import CaseBaseline, canonical_sha256
from testops.worker.outbox import dispatch_outbox_batch

ROOT = Path(__file__).resolve().parents[2]
RUNNER_HEADERS = {"X-Runner-Token": "integration-runner-token"}
BOOTSTRAP_TOKEN = "integration-bootstrap-token"
ADMIN_PASSWORD = "integration-admin-password"
PACKAGE_DIGEST = "sha256:" + "c" * 64


class RecordingPublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[object], str]] = []
        self.queues: list[str | None] = []

    def send_task(
        self,
        name: str,
        *,
        args: list[object],
        task_id: str,
        queue: str | None = None,
    ) -> object:
        self.calls.append((name, args, task_id))
        self.queues.append(queue)
        return {"task_id": task_id}


class RecordingArtifactStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, UUID, str]] = []

    def create_download_access(
        self,
        uri: str,
        *,
        run_id: UUID,
        filename: str,
    ) -> ArtifactAccess:
        self.calls.append((uri, run_id, filename))
        return ArtifactAccess(
            url="https://objects.example.invalid/signed-artifact",
            expires_in_seconds=120,
        )


class ControlPlaneApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "control-plane.sqlite3"
        settings = Settings(
            database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
            auto_create_schema=True,
            runner_callback_token=RUNNER_HEADERS["X-Runner-Token"],
            bootstrap_admin_token=BOOTSTRAP_TOKEN,
        )
        self.app = create_app(settings)
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        bootstrap = self.client.post(
            "/api/v1/auth/bootstrap",
            headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
            json={
                "username": "admin",
                "display_name": "Integration Admin",
                "password": ADMIN_PASSWORD,
            },
        )
        self.assertEqual(bootstrap.status_code, 201, bootstrap.text)
        login = self.client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": ADMIN_PASSWORD},
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    def _bootstrap(self) -> dict[str, str]:
        project_response = self.client.post(
            "/api/v1/projects",
            headers=self.headers,
            json={"key": "yanjia-ai-web", "name": "颜佳 AI Web"},
        )
        self.assertEqual(project_response.status_code, 201, project_response.text)
        project_id = project_response.json()["id"]

        target_response = self.client.post(
            f"/api/v1/projects/{project_id}/targets",
            headers=self.headers,
            json={
                "key": "web",
                "name": "Web 管理端",
                "target_type": "WEB",
                "browser": "chromium",
            },
        )
        self.assertEqual(target_response.status_code, 201, target_response.text)
        target_id = target_response.json()["id"]

        environment_response = self.client.post(
            f"/api/v1/projects/{project_id}/targets/{target_id}/environments",
            headers=self.headers,
            json={
                "key": "staging",
                "name": "测试环境",
                "web_config": {
                    "base_url": "https://example.invalid/login",
                    "headless": True,
                    "capture_trace": True,
                },
                "variables": [{"name": "STORE_NAME", "value": ""}],
                "secret_bindings": [
                    {
                        "name": "TEST_USERNAME",
                        "ref": "secret://yanjia/staging/test-username",
                    },
                    {
                        "name": "TEST_PASSWORD",
                        "ref": "secret://yanjia/staging/test-password",
                    },
                ],
            },
        )
        self.assertEqual(environment_response.status_code, 201, environment_response.text)
        environment_id = environment_response.json()["id"]

        baseline_directory = ROOT / "baselines/yanjia-ai-web/case-v1.0.1"
        baseline_document = json.loads(
            (baseline_directory / "case-baseline.json").read_text("utf-8")
        )
        manifest = json.loads((baseline_directory / "manifest.json").read_text("utf-8"))
        baseline_response = self.client.post(
            f"/api/v1/projects/{project_id}/baselines",
            headers=self.headers,
            json={
                "baseline": baseline_document,
                "digest": manifest["baseline"]["digest"],
            },
        )
        self.assertEqual(baseline_response.status_code, 201, baseline_response.text)
        baseline_id = baseline_response.json()["baseline_id"]

        package_response = self.client.post(
            f"/api/v1/projects/{project_id}/targets/{target_id}/automation-packages",
            headers=self.headers,
            json={"name": "yanjia-web", "version": "0.1.0", "digest": PACKAGE_DIGEST},
        )
        self.assertEqual(package_response.status_code, 201, package_response.text)
        package_id = package_response.json()["id"]
        return {
            "project_id": project_id,
            "target_id": target_id,
            "environment_id": environment_id,
            "baseline_id": baseline_id,
            "package_id": package_id,
        }

    @staticmethod
    def _run_payload(resources: dict[str, str], *case_codes: str) -> dict[str, object]:
        return {
            "project_id": resources["project_id"],
            "target_id": resources["target_id"],
            "environment_id": resources["environment_id"],
            "baseline_id": resources["baseline_id"],
            "automation_package_id": resources["package_id"],
            "case_codes": list(case_codes),
        }

    def test_full_run_creation_is_idempotent_and_writes_outbox(self) -> None:
        resources = self._bootstrap()
        request_headers = {**self.headers, "Idempotency-Key": "login-smoke-0001"}
        payload = self._run_payload(resources, "TC-LOGIN-007")

        first = self.client.post("/api/v1/runs", headers=request_headers, json=payload)
        self.assertEqual(first.status_code, 201, first.text)
        first_payload = first.json()
        self.assertEqual(first_payload["status"], "QUEUED")
        self.assertEqual(first_payload["case_count"], 1)

        replay = self.client.post("/api/v1/runs", headers=request_headers, json=payload)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["id"], first_payload["id"])

        detail = self.client.get(f"/api/v1/runs/{first_payload['id']}", headers=self.headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        snapshot = detail.json()["snapshot"]
        self.assertEqual(snapshot["case_baseline"]["version"], "case-v1.0.1")
        self.assertEqual([case["case_code"] for case in snapshot["cases"]], ["TC-LOGIN-007"])
        serialized = json.dumps(snapshot)
        self.assertIn("secret://yanjia/staging/test-password", serialized)
        self.assertNotIn("super-secret", serialized)

        async def inspect_database() -> tuple[int, int, dict[str, object]]:
            async with self.app.state.session_factory() as session:
                outbox_count = await session.scalar(
                    select(func.count()).select_from(DispatchOutboxRecord)
                )
                run_case_count = await session.scalar(
                    select(func.count()).select_from(RunCaseRecord)
                )
                outbox = await session.scalar(select(DispatchOutboxRecord))
                assert outbox is not None
                return int(outbox_count or 0), int(run_case_count or 0), outbox.payload

        outbox_count, run_case_count, outbox_payload = asyncio.run(inspect_database())
        self.assertEqual(outbox_count, 1)
        self.assertEqual(run_case_count, 1)
        self.assertEqual(
            outbox_payload["run_snapshot"]["run_id"],
            first_payload["id"],
        )

    def test_idempotency_key_rejects_a_different_request(self) -> None:
        resources = self._bootstrap()
        headers = {**self.headers, "Idempotency-Key": "same-key-0001"}
        first = self.client.post(
            "/api/v1/runs",
            headers=headers,
            json=self._run_payload(resources, "TC-LOGIN-001"),
        )
        self.assertEqual(first.status_code, 201, first.text)
        conflict = self.client.post(
            "/api/v1/runs",
            headers=headers,
            json=self._run_payload(resources, "TC-LOGIN-007"),
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)

    def test_project_execution_policy_enforces_in_flight_and_daily_quotas(self) -> None:
        resources = self._bootstrap()
        policy_url = f"/api/v1/projects/{resources['project_id']}/execution-policy"

        initial = self.client.get(policy_url, headers=self.headers)
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertEqual(initial.json()["max_in_flight_runs"], 20)
        self.assertEqual(initial.json()["max_daily_runs"], 500)
        self.assertEqual(initial.json()["quota_status"], "AVAILABLE")

        invalid = self.client.patch(
            policy_url,
            headers=self.headers,
            json={"max_in_flight_runs": 0},
        )
        self.assertEqual(invalid.status_code, 422, invalid.text)
        null_value = self.client.patch(
            policy_url,
            headers=self.headers,
            json={"max_daily_runs": None},
        )
        self.assertEqual(null_value.status_code, 422, null_value.text)

        updated = self.client.patch(
            policy_url,
            headers=self.headers,
            json={"max_in_flight_runs": 1, "max_daily_runs": 2},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["max_in_flight_runs"], 1)
        self.assertEqual(updated.json()["max_daily_runs"], 2)

        first_headers = {**self.headers, "Idempotency-Key": "quota-first-run"}
        first = self.client.post(
            "/api/v1/runs",
            headers=first_headers,
            json=self._run_payload(resources, "TC-LOGIN-001"),
        )
        self.assertEqual(first.status_code, 201, first.text)

        saturated = self.client.get(policy_url, headers=self.headers)
        self.assertEqual(saturated.status_code, 200, saturated.text)
        self.assertEqual(saturated.json()["in_flight_runs"], 1)
        self.assertEqual(saturated.json()["runs_created_today"], 1)
        self.assertEqual(saturated.json()["quota_status"], "BLOCKED")

        blocked_in_flight = self.client.post(
            "/api/v1/runs",
            headers={**self.headers, "Idempotency-Key": "quota-second-run"},
            json=self._run_payload(resources, "TC-LOGIN-007"),
        )
        self.assertEqual(blocked_in_flight.status_code, 409, blocked_in_flight.text)
        self.assertIn("in-flight Run quota", blocked_in_flight.json()["detail"])

        replay = self.client.post(
            "/api/v1/runs",
            headers=first_headers,
            json=self._run_payload(resources, "TC-LOGIN-001"),
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["id"], first.json()["id"])

        canceled_first = self.client.post(
            f"/api/v1/runs/{first.json()['id']}/cancel",
            headers=self.headers,
        )
        self.assertEqual(canceled_first.status_code, 200, canceled_first.text)

        second_headers = {**self.headers, "Idempotency-Key": "quota-second-run"}
        second = self.client.post(
            "/api/v1/runs",
            headers=second_headers,
            json=self._run_payload(resources, "TC-LOGIN-007"),
        )
        self.assertEqual(second.status_code, 201, second.text)
        self.client.post(f"/api/v1/runs/{second.json()['id']}/cancel", headers=self.headers)

        blocked_daily = self.client.post(
            "/api/v1/runs",
            headers={**self.headers, "Idempotency-Key": "quota-third-run"},
            json=self._run_payload(resources, "TC-LOGIN-001"),
        )
        self.assertEqual(blocked_daily.status_code, 409, blocked_daily.text)
        self.assertIn("daily Run quota", blocked_daily.json()["detail"])

        raised = self.client.patch(
            policy_url,
            headers=self.headers,
            json={"max_daily_runs": 3},
        )
        self.assertEqual(raised.status_code, 200, raised.text)
        third = self.client.post(
            "/api/v1/runs",
            headers={**self.headers, "Idempotency-Key": "quota-third-run"},
            json=self._run_payload(resources, "TC-LOGIN-001"),
        )
        self.assertEqual(third.status_code, 201, third.text)

        async def count_policy_audits() -> int:
            async with self.app.state.session_factory() as session:
                count = await session.scalar(
                    select(func.count())
                    .select_from(AuditLogRecord)
                    .where(AuditLogRecord.action == "project.execution_policy_updated")
                )
                return int(count or 0)

        self.assertEqual(asyncio.run(count_policy_audits()), 2)

    def test_runner_pool_waits_for_healthy_capable_capacity_and_resumes(self) -> None:
        resources = self._bootstrap()
        pool_response = self.client.post(
            "/api/v1/admin/runner-pools",
            headers=self.headers,
            json={
                "key": "web-chromium",
                "name": "Web Chromium Pool",
                "target_types": ["WEB"],
                "max_concurrency": 1,
            },
        )
        self.assertEqual(pool_response.status_code, 201, pool_response.text)
        pool = pool_response.json()
        self.assertEqual(pool["available_slots"], 0)

        bound = self.client.patch(
            f"/api/v1/projects/{resources['project_id']}/targets/{resources['target_id']}",
            headers=self.headers,
            json={"runner_pool_id": pool["id"]},
        )
        self.assertEqual(bound.status_code, 200, bound.text)
        self.assertEqual(bound.json()["runner_pool_id"], pool["id"])

        first = self.client.post(
            "/api/v1/runs",
            headers={**self.headers, "Idempotency-Key": "runner-pool-first"},
            json=self._run_payload(resources, "TC-LOGIN-001"),
        )
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["runner_pool_id"], pool["id"])
        self.assertEqual(first.json()["dispatch_state"], "PENDING")
        publisher = RecordingPublisher()
        base_time = datetime.now(UTC)

        async def dispatch(moment: datetime, *, limit: int = 20) -> object:
            return await dispatch_outbox_batch(
                self.app.state.session_factory,
                publisher,
                limit=limit,
                now=moment,
                capacity_poll_seconds=0.1,
            )

        no_worker = asyncio.run(dispatch(base_time))
        self.assertEqual(
            (no_worker.selected, no_worker.published, no_worker.failed, no_worker.waiting),
            (1, 0, 0, 1),
        )
        waiting = self.client.get(f"/api/v1/runs/{first.json()['id']}", headers=self.headers)
        self.assertEqual(waiting.json()["dispatch_state"], "WAITING")
        self.assertEqual(waiting.json()["dispatch_wait_reason"], "NO_HEALTHY_RUNNER")

        heartbeat_url = "/api/v1/internal/runner-workers/web-worker-01/heartbeat"
        mismatched = self.client.put(
            heartbeat_url,
            headers=RUNNER_HEADERS,
            json={
                "pool_key": "web-chromium",
                "display_name": "Web Worker 01",
                "runner_version": "0.10.0",
                "max_slots": 1,
                "capabilities": {
                    "target_types": ["WEB"],
                    "browsers": ["firefox"],
                    "labels": {"os": "linux"},
                },
            },
        )
        self.assertEqual(mismatched.status_code, 200, mismatched.text)
        capability_wait = asyncio.run(dispatch(base_time + timedelta(seconds=1)))
        self.assertEqual(capability_wait.waiting, 1)
        waiting = self.client.get(f"/api/v1/runs/{first.json()['id']}", headers=self.headers)
        self.assertEqual(
            waiting.json()["dispatch_wait_reason"],
            "RUNNER_CAPABILITY_MISMATCH",
        )

        matched = self.client.put(
            heartbeat_url,
            headers=RUNNER_HEADERS,
            json={
                "pool_key": "web-chromium",
                "display_name": "Web Worker 01",
                "runner_version": "0.10.0",
                "max_slots": 1,
                "capabilities": {
                    "target_types": ["WEB"],
                    "browsers": ["chromium"],
                    "labels": {"os": "linux"},
                },
            },
        )
        self.assertEqual(matched.status_code, 200, matched.text)
        published = asyncio.run(dispatch(base_time + timedelta(seconds=2)))
        self.assertEqual(
            (published.selected, published.published, published.waiting),
            (1, 1, 0),
        )
        self.assertEqual(publisher.queues, ["testops.pool.web-chromium"])
        dispatched = self.client.get(f"/api/v1/runs/{first.json()['id']}", headers=self.headers)
        self.assertEqual(dispatched.json()["dispatch_state"], "DISPATCHED")
        self.assertIsNone(dispatched.json()["dispatch_wait_reason"])

        second = self.client.post(
            "/api/v1/runs",
            headers={**self.headers, "Idempotency-Key": "runner-pool-second"},
            json=self._run_payload(resources, "TC-LOGIN-007"),
        )
        self.assertEqual(second.status_code, 201, second.text)
        exhausted = asyncio.run(dispatch(base_time + timedelta(seconds=3)))
        self.assertEqual(exhausted.waiting, 1)
        second_waiting = self.client.get(
            f"/api/v1/runs/{second.json()['id']}", headers=self.headers
        )
        self.assertEqual(
            second_waiting.json()["dispatch_wait_reason"],
            "RUNNER_POOL_CAPACITY_EXHAUSTED",
        )

        canceled = self.client.post(
            f"/api/v1/runs/{first.json()['id']}/cancel",
            headers=self.headers,
        )
        self.assertEqual(canceled.status_code, 200, canceled.text)
        resumed = asyncio.run(dispatch(base_time + timedelta(seconds=4), limit=1))
        self.assertEqual((resumed.published, resumed.waiting), (1, 0))
        self.assertEqual(publisher.queues[-1], "testops.pool.web-chromium")

        pools = self.client.get("/api/v1/admin/runner-pools", headers=self.headers)
        self.assertEqual(pools.status_code, 200, pools.text)
        self.assertEqual(pools.json()[0]["healthy_workers"], 1)
        self.assertEqual(pools.json()[0]["active_leases"], 1)
        self.assertEqual(pools.json()[0]["available_slots"], 0)
        workers = self.client.get("/api/v1/admin/runner-workers", headers=self.headers)
        self.assertEqual(workers.status_code, 200, workers.text)
        self.assertEqual(workers.json()[0]["health"], "ONLINE")
        catalog = self.client.get("/api/v1/runner-pools/catalog", headers=self.headers)
        self.assertEqual(catalog.status_code, 200, catalog.text)
        self.assertEqual(catalog.json()[0]["key"], "web-chromium")

    def test_published_baseline_is_idempotent_but_immutable(self) -> None:
        resources = self._bootstrap()
        baseline_directory = ROOT / "baselines/yanjia-ai-web/case-v1.0.1"
        document = json.loads((baseline_directory / "case-baseline.json").read_text("utf-8"))
        manifest = json.loads((baseline_directory / "manifest.json").read_text("utf-8"))
        url = f"/api/v1/projects/{resources['project_id']}/baselines"

        replay = self.client.post(
            url,
            headers=self.headers,
            json={"baseline": document, "digest": manifest["baseline"]["digest"]},
        )
        self.assertEqual(replay.status_code, 200, replay.text)

        changed = CaseBaseline.model_validate(document)
        changed_cases = list(changed.cases)
        changed_cases[0] = changed_cases[0].model_copy(update={"title": "不可覆盖的变化"})
        changed = changed.model_copy(update={"cases": tuple(changed_cases)})
        conflict = self.client.post(
            url,
            headers=self.headers,
            json={
                "baseline": changed.model_dump(mode="json", exclude_none=True),
                "digest": canonical_sha256(changed),
            },
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)

    def test_queued_run_can_be_canceled_idempotently(self) -> None:
        resources = self._bootstrap()
        created = self.client.post(
            "/api/v1/runs",
            headers={**self.headers, "Idempotency-Key": "cancel-run-0001"},
            json=self._run_payload(resources, "TC-LOGIN-003"),
        )
        self.assertEqual(created.status_code, 201, created.text)
        run_id = created.json()["id"]
        initial_events = self.client.get(
            f"/api/v1/runs/{run_id}/events",
            headers=self.headers,
        )
        self.assertEqual(initial_events.status_code, 200, initial_events.text)
        self.assertEqual(
            [event["event_type"] for event in initial_events.json()],
            ["run_created"],
        )

        canceled = self.client.post(f"/api/v1/runs/{run_id}/cancel", headers=self.headers)
        self.assertEqual(canceled.status_code, 200, canceled.text)
        self.assertEqual(canceled.json()["status"], "CANCELED")
        self.assertTrue(canceled.json()["cancel_requested"])
        replay = self.client.post(f"/api/v1/runs/{run_id}/cancel", headers=self.headers)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["status"], "CANCELED")

        async def counts() -> tuple[int, int]:
            async with self.app.state.session_factory() as session:
                outbox_count = await session.scalar(
                    select(func.count()).select_from(DispatchOutboxRecord)
                )
                audit_count = await session.scalar(select(func.count()).select_from(AuditLogRecord))
                return int(outbox_count or 0), int(audit_count or 0)

        outbox_count, audit_count = asyncio.run(counts())
        self.assertEqual(outbox_count, 2)
        self.assertGreaterEqual(audit_count, 7)

    def test_run_filters_batch_cancel_and_snapshot_reruns(self) -> None:
        resources = self._bootstrap()
        source_response = self.client.post(
            "/api/v1/runs",
            headers={**self.headers, "Idempotency-Key": "operations-source-0001"},
            json=self._run_payload(resources, "TC-LOGIN-001", "TC-LOGIN-003"),
        )
        self.assertEqual(source_response.status_code, 201, source_response.text)
        source_id = source_response.json()["id"]
        source_detail = self.client.get(f"/api/v1/runs/{source_id}", headers=self.headers)
        source_snapshot = source_detail.json()["snapshot"]

        for run_status in ("PREPARING", "RUNNING"):
            callback = self.client.post(
                f"/api/v1/internal/runs/{source_id}/status",
                headers=RUNNER_HEADERS,
                json={"status": run_status},
            )
            self.assertEqual(callback.status_code, 200, callback.text)
        started = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
        finished = started + timedelta(seconds=2)
        case_results = []
        for index, case in enumerate(source_snapshot["cases"]):
            failed = index == 0
            case_results.append(
                {
                    "case_id": case["case_id"],
                    "case_code": case["case_code"],
                    "status": "FAILED" if failed else "PASSED",
                    "started_at": started.isoformat(),
                    "finished_at": finished.isoformat(),
                    "duration_ms": 1000,
                    **(
                        {
                            "failure_category": "ASSERTION",
                            "error_message": "expected failure for rerun test",
                        }
                        if failed
                        else {}
                    ),
                }
            )
        result = self.client.post(
            f"/api/v1/internal/runs/{source_id}/result",
            headers=RUNNER_HEADERS,
            json={
                "schema_version": "1.0",
                "run_id": source_id,
                "status": "FAILED",
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "runner_version": "0.10.0",
                "case_results": case_results,
                "artifacts": [],
            },
        )
        self.assertEqual(result.status_code, 200, result.text)

        filtered = self.client.get(
            f"/api/v1/projects/{resources['project_id']}/runs?status=FAILED&case_code=TC-LOGIN-001",
            headers=self.headers,
        )
        self.assertEqual(filtered.status_code, 200, filtered.text)
        self.assertEqual(filtered.json()["total"], 1)
        self.assertEqual(filtered.json()["items"][0]["id"], source_id)

        changed_environment = self.client.patch(
            f"/api/v1/projects/{resources['project_id']}/targets/{resources['target_id']}"
            f"/environments/{resources['environment_id']}",
            headers=self.headers,
            json={"web_config": {"base_url": "https://changed.example.invalid/login"}},
        )
        self.assertEqual(changed_environment.status_code, 200, changed_environment.text)
        self.assertNotEqual(
            changed_environment.json()["config_hash"], source_snapshot["config_hash"]
        )

        failed_rerun = self.client.post(
            f"/api/v1/runs/{source_id}/rerun",
            headers={**self.headers, "Idempotency-Key": "failed-rerun-0001"},
            json={"mode": "FAILED_ONLY"},
        )
        self.assertEqual(failed_rerun.status_code, 201, failed_rerun.text)
        self.assertEqual(failed_rerun.json()["case_count"], 1)
        self.assertEqual(failed_rerun.json()["source_run_id"], source_id)
        self.assertEqual(failed_rerun.json()["retry_mode"], "FAILED_ONLY")
        failed_rerun_id = failed_rerun.json()["id"]
        failed_detail = self.client.get(
            f"/api/v1/runs/{failed_rerun_id}",
            headers=self.headers,
        )
        self.assertEqual(
            [case["case_code"] for case in failed_detail.json()["snapshot"]["cases"]],
            [source_snapshot["cases"][0]["case_code"]],
        )
        self.assertEqual(
            failed_detail.json()["snapshot"]["config_hash"],
            source_snapshot["config_hash"],
        )
        self.assertEqual(
            failed_detail.json()["snapshot"]["web_config"],
            source_snapshot["web_config"],
        )
        replay = self.client.post(
            f"/api/v1/runs/{source_id}/rerun",
            headers={**self.headers, "Idempotency-Key": "failed-rerun-0001"},
            json={"mode": "FAILED_ONLY"},
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["id"], failed_rerun_id)

        full_rerun = self.client.post(
            f"/api/v1/runs/{source_id}/rerun",
            headers={**self.headers, "Idempotency-Key": "full-rerun-0001"},
            json={"mode": "FULL"},
        )
        self.assertEqual(full_rerun.status_code, 201, full_rerun.text)
        self.assertEqual(full_rerun.json()["case_count"], 2)
        full_rerun_id = full_rerun.json()["id"]

        nonterminal = self.client.post(
            f"/api/v1/runs/{failed_rerun_id}/rerun",
            headers={**self.headers, "Idempotency-Key": "nested-rerun-0001"},
            json={"mode": "FULL"},
        )
        self.assertEqual(nonterminal.status_code, 409, nonterminal.text)

        duplicate_batch = self.client.post(
            f"/api/v1/projects/{resources['project_id']}/runs/batch-cancel",
            headers=self.headers,
            json={"run_ids": [failed_rerun_id, failed_rerun_id]},
        )
        self.assertEqual(duplicate_batch.status_code, 422, duplicate_batch.text)
        batch = self.client.post(
            f"/api/v1/projects/{resources['project_id']}/runs/batch-cancel",
            headers=self.headers,
            json={"run_ids": [source_id, failed_rerun_id, full_rerun_id]},
        )
        self.assertEqual(batch.status_code, 200, batch.text)
        self.assertEqual(batch.json()["requested"], 3)
        self.assertEqual(batch.json()["changed"], 2)
        self.assertEqual(
            [item["status"] for item in batch.json()["items"]],
            ["FAILED", "CANCELED", "CANCELED"],
        )
        batch_replay = self.client.post(
            f"/api/v1/projects/{resources['project_id']}/runs/batch-cancel",
            headers=self.headers,
            json={"run_ids": [failed_rerun_id, full_rerun_id]},
        )
        self.assertEqual(batch_replay.status_code, 200, batch_replay.text)
        self.assertEqual(batch_replay.json()["changed"], 0)

        lineage = self.client.get(
            f"/api/v1/projects/{resources['project_id']}/runs"
            f"?status=CANCELED&source_run_id={source_id}",
            headers=self.headers,
        )
        self.assertEqual(lineage.status_code, 200, lineage.text)
        self.assertEqual(lineage.json()["total"], 2)

        async def operation_audits() -> tuple[int, int]:
            async with self.app.state.session_factory() as session:
                reruns = await session.scalar(
                    select(func.count())
                    .select_from(AuditLogRecord)
                    .where(AuditLogRecord.action == "run.rerun_created")
                )
                cancellations = await session.scalar(
                    select(func.count())
                    .select_from(AuditLogRecord)
                    .where(AuditLogRecord.action == "run.cancel_requested")
                )
                return int(reruns or 0), int(cancellations or 0)

        self.assertEqual(asyncio.run(operation_audits()), (2, 2))

    def test_runner_callbacks_persist_an_immutable_result_and_artifact(self) -> None:
        resources = self._bootstrap()
        created = self.client.post(
            "/api/v1/runs",
            headers={**self.headers, "Idempotency-Key": "runner-result-0001"},
            json=self._run_payload(resources, "TC-LOGIN-007"),
        )
        self.assertEqual(created.status_code, 201, created.text)
        run_id = created.json()["id"]

        unauthorized = self.client.post(
            f"/api/v1/internal/runs/{run_id}/status",
            headers={"X-Runner-Token": "wrong-token"},
            json={"status": "PREPARING"},
        )
        self.assertEqual(unauthorized.status_code, 401, unauthorized.text)

        preparing = self.client.post(
            f"/api/v1/internal/runs/{run_id}/status",
            headers=RUNNER_HEADERS,
            json={"status": "PREPARING"},
        )
        self.assertEqual(preparing.status_code, 200, preparing.text)
        self.assertTrue(preparing.json()["changed"])
        replay_status = self.client.post(
            f"/api/v1/internal/runs/{run_id}/status",
            headers=RUNNER_HEADERS,
            json={"status": "PREPARING"},
        )
        self.assertFalse(replay_status.json()["changed"])
        running = self.client.post(
            f"/api/v1/internal/runs/{run_id}/status",
            headers=RUNNER_HEADERS,
            json={"status": "RUNNING"},
        )
        self.assertEqual(running.status_code, 200, running.text)

        snapshot = self.client.get(f"/api/v1/runs/{run_id}", headers=self.headers).json()[
            "snapshot"
        ]
        case = snapshot["cases"][0]
        started_at = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)
        finished_at = started_at + timedelta(milliseconds=125)
        started_at_text = started_at.isoformat().replace("+00:00", "Z")
        finished_at_text = finished_at.isoformat().replace("+00:00", "Z")
        runner_events = [
            {"event": "run_started", "run_id": run_id, "at": started_at_text},
            {
                "event": "case_started",
                "run_id": run_id,
                "case_code": case["case_code"],
                "at": started_at_text,
            },
            {
                "event": "case_finished",
                "run_id": run_id,
                "case_code": case["case_code"],
                "status": "PASSED",
                "at": finished_at_text,
            },
            {
                "event": "run_finished",
                "run_id": run_id,
                "status": "PASSED",
                "at": finished_at_text,
            },
        ]
        unauthorized_event = self.client.post(
            f"/api/v1/internal/runs/{run_id}/events",
            headers={"X-Runner-Token": "wrong-token"},
            json=runner_events[0],
        )
        self.assertEqual(unauthorized_event.status_code, 401, unauthorized_event.text)
        for event in runner_events:
            accepted_event = self.client.post(
                f"/api/v1/internal/runs/{run_id}/events",
                headers=RUNNER_HEADERS,
                json=event,
            )
            self.assertEqual(accepted_event.status_code, 201, accepted_event.text)
        replayed_event = self.client.post(
            f"/api/v1/internal/runs/{run_id}/events",
            headers=RUNNER_HEADERS,
            json=runner_events[1],
        )
        self.assertEqual(replayed_event.status_code, 200, replayed_event.text)
        unknown_case_event = {
            **runner_events[1],
            "case_code": "TC-NOT-IN-SNAPSHOT",
        }
        rejected_event = self.client.post(
            f"/api/v1/internal/runs/{run_id}/events",
            headers=RUNNER_HEADERS,
            json=unknown_case_event,
        )
        self.assertEqual(rejected_event.status_code, 422, rejected_event.text)
        artifact_id = uuid4()
        result_document = {
            "schema_version": "1.0",
            "run_id": run_id,
            "status": "PASSED",
            "started_at": started_at_text,
            "finished_at": finished_at_text,
            "runner_version": "0.1.0",
            "case_results": [
                {
                    "case_id": case["case_id"],
                    "case_code": case["case_code"],
                    "status": "PASSED",
                    "started_at": started_at_text,
                    "finished_at": finished_at_text,
                    "duration_ms": 125,
                    "artifact_ids": [str(artifact_id)],
                }
            ],
            "artifacts": [
                {
                    "artifact_id": str(artifact_id),
                    "kind": "LOG",
                    "name": "events.jsonl",
                    "uri": f"runs/{run_id}/events.jsonl",
                    "digest": "sha256:" + "d" * 64,
                    "size_bytes": 42,
                }
            ],
        }
        accepted = self.client.post(
            f"/api/v1/internal/runs/{run_id}/result",
            headers=RUNNER_HEADERS,
            json=result_document,
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertTrue(accepted.json()["created"])
        self.assertEqual(accepted.json()["status"], "PASSED")

        replay = self.client.post(
            f"/api/v1/internal/runs/{run_id}/result",
            headers=RUNNER_HEADERS,
            json=result_document,
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertFalse(replay.json()["created"])
        changed_result = {**result_document, "runner_version": "0.1.1"}
        conflict = self.client.post(
            f"/api/v1/internal/runs/{run_id}/result",
            headers=RUNNER_HEADERS,
            json=changed_result,
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)

        detail = self.client.get(f"/api/v1/runs/{run_id}", headers=self.headers)
        returned_result = detail.json()["result"]
        self.assertEqual(returned_result["run_id"], result_document["run_id"])
        self.assertEqual(returned_result["status"], "PASSED")
        self.assertEqual(returned_result["case_results"][0]["artifact_ids"], [str(artifact_id)])
        self.assertEqual(returned_result["artifacts"], result_document["artifacts"])
        self.assertEqual(detail.json()["result_digest"], accepted.json()["result_digest"])
        self.assertEqual(detail.json()["cases"][0]["status"], "PASSED")
        self.assertEqual(detail.json()["artifacts"][0]["artifact_id"], str(artifact_id))

        events = self.client.get(f"/api/v1/runs/{run_id}/events", headers=self.headers)
        self.assertEqual(events.status_code, 200, events.text)
        event_documents = events.json()
        self.assertEqual(
            [event["sequence"] for event in event_documents],
            list(range(1, len(event_documents) + 1)),
        )
        self.assertIn("result_recorded", [event["event_type"] for event in event_documents])
        after = self.client.get(
            f"/api/v1/runs/{run_id}/events?after_sequence=4",
            headers=self.headers,
        )
        self.assertTrue(all(event["sequence"] > 4 for event in after.json()))
        stream = self.client.get(
            f"/api/v1/runs/{run_id}/events/stream?follow=false",
            headers=self.headers,
        )
        self.assertEqual(stream.status_code, 200, stream.text)
        self.assertEqual(stream.headers["content-type"], "text/event-stream; charset=utf-8")
        self.assertIn("event: run_created", stream.text)

        artifact_store = RecordingArtifactStore()
        self.app.state.artifact_store = artifact_store
        unauthorized_access = self.client.get(
            f"/api/v1/runs/{run_id}/artifacts/{artifact_id}/access"
        )
        self.assertEqual(unauthorized_access.status_code, 401, unauthorized_access.text)
        access = self.client.get(
            f"/api/v1/runs/{run_id}/artifacts/{artifact_id}/access",
            headers=self.headers,
        )
        self.assertEqual(access.status_code, 200, access.text)
        self.assertEqual(access.json()["url"], "https://objects.example.invalid/signed-artifact")
        self.assertEqual(artifact_store.calls[0][1:], (UUID(run_id), "events.jsonl"))

        async def inspect_database() -> tuple[int, str, int, int, int]:
            async with self.app.state.session_factory() as session:
                artifact_count = await session.scalar(
                    select(func.count()).select_from(ArtifactRecord)
                )
                run_case = await session.scalar(
                    select(RunCaseRecord).where(RunCaseRecord.run_id == UUID(run_id))
                )
                status_audits = await session.scalar(
                    select(func.count())
                    .select_from(AuditLogRecord)
                    .where(AuditLogRecord.action == "run.status_changed")
                )
                event_count = await session.scalar(select(func.count()).select_from(RunEventRecord))
                artifact_access_audits = await session.scalar(
                    select(func.count())
                    .select_from(AuditLogRecord)
                    .where(AuditLogRecord.action == "artifact.access_granted")
                )
                assert run_case is not None
                return (
                    int(artifact_count or 0),
                    run_case.status,
                    int(status_audits or 0),
                    int(event_count or 0),
                    int(artifact_access_audits or 0),
                )

        artifact_count, case_status, status_audits, event_count, access_audits = asyncio.run(
            inspect_database()
        )
        self.assertEqual(artifact_count, 1)
        self.assertEqual(case_status, "PASSED")
        self.assertEqual(status_audits, 2)
        self.assertGreaterEqual(event_count, 8)
        self.assertEqual(access_audits, 1)

    def test_outbox_dispatches_run_snapshot_once_with_a_stable_task_id(self) -> None:
        resources = self._bootstrap()
        created = self.client.post(
            "/api/v1/runs",
            headers={**self.headers, "Idempotency-Key": "outbox-run-0001"},
            json=self._run_payload(resources, "TC-LOGIN-001"),
        )
        self.assertEqual(created.status_code, 201, created.text)
        run_id = created.json()["id"]
        publisher = RecordingPublisher()

        async def dispatch_twice() -> tuple[object, object, str]:
            first = await dispatch_outbox_batch(self.app.state.session_factory, publisher)
            second = await dispatch_outbox_batch(self.app.state.session_factory, publisher)
            async with self.app.state.session_factory() as session:
                outbox = await session.scalar(select(DispatchOutboxRecord))
                assert outbox is not None
                return first, second, outbox.status

        first, second, outbox_status = asyncio.run(dispatch_twice())
        self.assertEqual((first.selected, first.published, first.failed), (1, 1, 0))
        self.assertEqual((second.selected, second.published, second.failed), (0, 0, 0))
        self.assertEqual(outbox_status, "PUBLISHED")
        self.assertEqual(len(publisher.calls), 1)
        task_name, args, task_id = publisher.calls[0]
        self.assertEqual(task_name, "testops.execute_run")
        self.assertEqual(task_id, f"run:{run_id}")
        self.assertEqual(args[0]["run_id"], run_id)

    def test_ready_lists_and_validation_fail_closed(self) -> None:
        resources = self._bootstrap()
        self.assertEqual(self.client.get("/readyz").status_code, 200)
        projects = self.client.get("/api/v1/projects", headers=self.headers)
        self.assertEqual(projects.status_code, 200)
        self.assertEqual(len(projects.json()), 1)
        baselines = self.client.get(
            f"/api/v1/projects/{resources['project_id']}/baselines",
            headers=self.headers,
        )
        self.assertEqual(baselines.json()[0]["case_count"], 92)

        missing_actor = self.client.post(
            "/api/v1/projects",
            json={"key": "missing-actor", "name": "Missing actor"},
        )
        self.assertEqual(missing_actor.status_code, 401)
        unsafe_environment = self.client.post(
            f"/api/v1/projects/{resources['project_id']}/targets/"
            f"{resources['target_id']}/environments",
            headers=self.headers,
            json={
                "key": "unsafe",
                "name": "Unsafe",
                "web_config": {"base_url": "https://example.invalid"},
                "variables": [{"name": "ACCESS_TOKEN", "value": "raw-secret"}],
            },
        )
        self.assertEqual(unsafe_environment.status_code, 422)


if __name__ == "__main__":
    unittest.main()
