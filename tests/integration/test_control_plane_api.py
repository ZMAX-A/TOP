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
    ProjectMemberRecord,
    QualityAlertStateRecord,
    QualityWebhookConfigRecord,
    QualityWebhookDeliveryRecord,
    RegressionScheduleFiringRecord,
    RegressionScheduleRecord,
    RunCaseRecord,
    RunEventRecord,
    RunnerSlotLeaseRecord,
    RunnerWorkerRecord,
)
from testops.api.persistence import TestRunRecord as RunRecord
from testops.api.quality_alert_services import evaluate_quality_alert_batch
from testops.api.reliability_services import process_run_reliability
from testops.api.schedule_services import process_due_schedules
from testops.contracts import CaseBaseline, canonical_sha256
from testops.worker.outbox import dispatch_outbox_batch
from testops.worker.quality_webhooks import dispatch_quality_webhook_batch

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


class RecordingQualityWebhookSender:
    def __init__(self, *statuses: int):
        self.statuses = list(statuses or (204,))
        self.calls: list[tuple[str, str, str | None]] = []

    async def send(
        self,
        config: QualityWebhookConfigRecord,
        delivery: QualityWebhookDeliveryRecord,
    ) -> int:
        self.calls.append((config.endpoint_url, delivery.event_type, config.signing_secret_name))
        return self.statuses.pop(0)


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

    def test_automation_package_draft_validation_activation_and_retirement(self) -> None:
        resources = self._bootstrap()
        packages_url = (
            f"/api/v1/projects/{resources['project_id']}/targets/"
            f"{resources['target_id']}/automation-packages"
        )
        draft_payload = {
            "name": "yanjia-web",
            "version": "0.2.0",
            "digest": "sha256:" + "9" * 64,
            "runner_type": "WEB_PLAYWRIGHT",
            "image_repository": "registry.example.invalid/testops/yanjia-web",
            "supersedes_id": resources["package_id"],
        }
        invalid_repository = self.client.post(
            f"{packages_url}/drafts",
            headers=self.headers,
            json={**draft_payload, "image_repository": "yanjia-web:latest"},
        )
        self.assertEqual(invalid_repository.status_code, 422, invalid_repository.text)

        drafted = self.client.post(
            f"{packages_url}/drafts",
            headers=self.headers,
            json=draft_payload,
        )
        self.assertEqual(drafted.status_code, 201, drafted.text)
        draft = drafted.json()
        self.assertEqual(draft["status"], "DRAFT")
        self.assertEqual(draft["runner_type"], "WEB_PLAYWRIGHT")
        self.assertEqual(draft["supersedes_id"], resources["package_id"])
        self.assertIsNone(draft["validated_run_id"])
        duplicate = self.client.post(
            f"{packages_url}/drafts",
            headers=self.headers,
            json=draft_payload,
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.text)

        draft_run_payload = {
            **self._run_payload(resources),
            "automation_package_id": draft["id"],
        }
        ordinary_draft_run = self.client.post(
            "/api/v1/runs",
            headers={**self.headers, "Idempotency-Key": "draft-ordinary-run"},
            json=draft_run_payload,
        )
        self.assertEqual(ordinary_draft_run.status_code, 422, ordinary_draft_run.text)

        validation_url = f"{packages_url}/{draft['id']}/validation-runs"
        validation_payload = {
            "environment_id": resources["environment_id"],
            "baseline_id": resources["baseline_id"],
        }
        validation = self.client.post(
            validation_url,
            headers={**self.headers, "Idempotency-Key": "package-validation-0001"},
            json=validation_payload,
        )
        self.assertEqual(validation.status_code, 201, validation.text)
        validation_run = validation.json()
        self.assertEqual(validation_run["automation_package_id"], draft["id"])
        self.assertEqual(validation_run["case_count"], 89)
        replayed_validation = self.client.post(
            validation_url,
            headers={**self.headers, "Idempotency-Key": "package-validation-0001"},
            json=validation_payload,
        )
        self.assertEqual(replayed_validation.status_code, 200, replayed_validation.text)
        self.assertEqual(replayed_validation.json()["id"], validation_run["id"])

        activation_url = f"{packages_url}/{draft['id']}/activate"
        premature_activation = self.client.post(
            activation_url,
            headers=self.headers,
            json={"validation_run_id": validation_run["id"]},
        )
        self.assertEqual(premature_activation.status_code, 409, premature_activation.text)

        async def complete_validation_run() -> None:
            async with self.app.state.session_factory() as session:
                record = await session.get(RunRecord, UUID(validation_run["id"]))
                assert record is not None
                record.status = "PASSED"
                record.result_digest = "sha256:" + "8" * 64
                record.result_document = {"status": "PASSED"}
                record.finished_at = datetime.now(UTC)
                await session.commit()

        asyncio.run(complete_validation_run())
        activated = self.client.post(
            activation_url,
            headers=self.headers,
            json={"validation_run_id": validation_run["id"]},
        )
        self.assertEqual(activated.status_code, 200, activated.text)
        active_package = activated.json()
        self.assertEqual(active_package["status"], "ACTIVE")
        self.assertEqual(active_package["validated_run_id"], validation_run["id"])
        self.assertIsNotNone(active_package["activated_at"])
        repeated_activation = self.client.post(
            activation_url,
            headers=self.headers,
            json={"validation_run_id": validation_run["id"]},
        )
        self.assertEqual(repeated_activation.status_code, 200, repeated_activation.text)

        ordinary_active_run = self.client.post(
            "/api/v1/runs",
            headers={**self.headers, "Idempotency-Key": "active-package-run"},
            json=draft_run_payload,
        )
        self.assertEqual(ordinary_active_run.status_code, 201, ordinary_active_run.text)
        package_detail = self.client.get(
            f"{packages_url}/{draft['id']}",
            headers=self.headers,
        )
        self.assertEqual(package_detail.status_code, 200, package_detail.text)
        self.assertEqual(package_detail.json()["digest"], draft_payload["digest"])

        schedule_url = f"/api/v1/projects/{resources['project_id']}/regression-schedules"
        schedule = self.client.post(
            schedule_url,
            headers=self.headers,
            json={
                "key": "package-lifecycle-nightly",
                "name": "Package lifecycle nightly",
                "target_id": resources["target_id"],
                "environment_id": resources["environment_id"],
                "baseline_id": resources["baseline_id"],
                "automation_package_id": draft["id"],
                "cron_expression": "0 2 * * *",
                "timezone": "Asia/Shanghai",
            },
        )
        self.assertEqual(schedule.status_code, 201, schedule.text)
        deprecate_url = f"{packages_url}/{draft['id']}/deprecate"
        in_use_deprecation = self.client.post(
            deprecate_url,
            headers=self.headers,
            json={"reason": "superseded after rollout"},
        )
        self.assertEqual(in_use_deprecation.status_code, 409, in_use_deprecation.text)
        paused_schedule = self.client.patch(
            f"{schedule_url}/{schedule.json()['id']}",
            headers=self.headers,
            json={"status": "PAUSED"},
        )
        self.assertEqual(paused_schedule.status_code, 200, paused_schedule.text)
        deprecated = self.client.post(
            deprecate_url,
            headers=self.headers,
            json={"reason": "superseded after rollout"},
        )
        self.assertEqual(deprecated.status_code, 200, deprecated.text)
        self.assertEqual(deprecated.json()["status"], "DEPRECATED")
        deprecated_run = self.client.post(
            "/api/v1/runs",
            headers={**self.headers, "Idempotency-Key": "deprecated-package-run"},
            json=draft_run_payload,
        )
        self.assertEqual(deprecated_run.status_code, 422, deprecated_run.text)
        revoked = self.client.post(
            f"{packages_url}/{draft['id']}/revoke",
            headers=self.headers,
            json={"reason": "dependency security incident"},
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(revoked.json()["status"], "REVOKED")
        repeated_revoke = self.client.post(
            f"{packages_url}/{draft['id']}/revoke",
            headers=self.headers,
            json={"reason": "dependency security incident"},
        )
        self.assertEqual(repeated_revoke.status_code, 200, repeated_revoke.text)

        async def lifecycle_audits() -> set[str]:
            async with self.app.state.session_factory() as session:
                rows = await session.scalars(
                    select(AuditLogRecord.action).where(AuditLogRecord.resource_id == draft["id"])
                )
                return set(rows)

        self.assertTrue(
            {
                "automation_package.draft_created",
                "automation_package.validation_run_created",
                "automation_package.activated",
                "automation_package.deprecated",
                "automation_package.revoked",
            }.issubset(asyncio.run(lifecycle_audits()))
        )

        project_admin = self.client.post(
            "/api/v1/users",
            headers=self.headers,
            json={
                "username": "package-admin",
                "display_name": "Package Admin",
                "password": "package-admin-password",
            },
        )
        self.assertEqual(project_admin.status_code, 201, project_admin.text)
        membership = self.client.put(
            f"/api/v1/projects/{resources['project_id']}/members",
            headers=self.headers,
            json={"user_id": project_admin.json()["id"], "role": "PROJECT_ADMIN"},
        )
        self.assertEqual(membership.status_code, 200, membership.text)
        project_admin_login = self.client.post(
            "/api/v1/auth/login",
            json={"username": "package-admin", "password": "package-admin-password"},
        )
        project_admin_headers = {
            "Authorization": f"Bearer {project_admin_login.json()['access_token']}"
        }
        delegated_payload = {
            "name": "delegated-web",
            "version": "1.0.0",
            "digest": "sha256:" + "7" * 64,
            "image_repository": "registry.example.invalid/testops/delegated-web",
        }
        denied_legacy_registration = self.client.post(
            packages_url,
            headers=project_admin_headers,
            json=delegated_payload,
        )
        self.assertEqual(
            denied_legacy_registration.status_code,
            403,
            denied_legacy_registration.text,
        )
        delegated_draft = self.client.post(
            f"{packages_url}/drafts",
            headers=project_admin_headers,
            json=delegated_payload,
        )
        self.assertEqual(delegated_draft.status_code, 201, delegated_draft.text)
        self.assertEqual(delegated_draft.json()["status"], "DRAFT")

    def test_project_creation_persists_creator_membership_and_audit(self) -> None:
        first = self.client.post(
            "/api/v1/projects",
            headers=self.headers,
            json={"key": "project-transaction", "name": "Project Transaction"},
        )
        self.assertEqual(first.status_code, 201, first.text)
        project_id = UUID(first.json()["id"])

        duplicate = self.client.post(
            "/api/v1/projects",
            headers=self.headers,
            json={"key": "project-transaction", "name": "Duplicate Project"},
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        self.assertEqual(duplicate.json()["detail"], "project key already exists")

        async def persisted_dependencies() -> tuple[int, int]:
            async with self.app.state.session_factory() as session:
                member_count = await session.scalar(
                    select(func.count())
                    .select_from(ProjectMemberRecord)
                    .where(ProjectMemberRecord.project_id == project_id)
                )
                audit_count = await session.scalar(
                    select(func.count())
                    .select_from(AuditLogRecord)
                    .where(
                        AuditLogRecord.project_id == project_id,
                        AuditLogRecord.action == "project.created",
                    )
                )
                return int(member_count or 0), int(audit_count or 0)

        self.assertEqual(asyncio.run(persisted_dependencies()), (1, 1))

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
        self.assertEqual(initial.json()["run_timeout_seconds"], 3600)
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
        invalid_timeout = self.client.patch(
            policy_url,
            headers=self.headers,
            json={"run_timeout_seconds": 59},
        )
        self.assertEqual(invalid_timeout.status_code, 422, invalid_timeout.text)

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

    def test_run_reliability_times_out_and_recovers_runner_leases(self) -> None:
        resources = self._bootstrap()
        policy = self.client.patch(
            f"/api/v1/projects/{resources['project_id']}/execution-policy",
            headers=self.headers,
            json={"run_timeout_seconds": 60},
        )
        self.assertEqual(policy.status_code, 200, policy.text)
        self.assertEqual(policy.json()["run_timeout_seconds"], 60)
        pool = self.client.post(
            "/api/v1/admin/runner-pools",
            headers=self.headers,
            json={
                "key": "reliability-web",
                "name": "Reliability Web Pool",
                "target_types": ["WEB"],
                "max_concurrency": 1,
            },
        )
        self.assertEqual(pool.status_code, 201, pool.text)
        bound = self.client.patch(
            f"/api/v1/projects/{resources['project_id']}/targets/{resources['target_id']}",
            headers=self.headers,
            json={"runner_pool_id": pool.json()["id"]},
        )
        self.assertEqual(bound.status_code, 200, bound.text)
        heartbeat_url = "/api/v1/internal/runner-workers/reliability-worker/heartbeat"
        heartbeat_payload = {
            "pool_key": "reliability-web",
            "display_name": "Reliability Worker",
            "runner_version": "0.12.0",
            "max_slots": 1,
            "capabilities": {
                "target_types": ["WEB"],
                "browsers": ["chromium"],
                "labels": {"purpose": "reliability-test"},
            },
        }
        heartbeat = self.client.put(
            heartbeat_url,
            headers=RUNNER_HEADERS,
            json=heartbeat_payload,
        )
        self.assertEqual(heartbeat.status_code, 200, heartbeat.text)
        publisher = RecordingPublisher()

        async def dispatch() -> object:
            return await dispatch_outbox_batch(
                self.app.state.session_factory,
                publisher,
                capacity_poll_seconds=0.1,
            )

        async def expire_run_timeout(run_id: str, moment: datetime) -> None:
            async with self.app.state.session_factory() as session:
                run = await session.get(RunRecord, UUID(run_id))
                assert run is not None
                run.timeout_at = moment - timedelta(seconds=1)
                await session.commit()

        async def expire_worker_heartbeat(moment: datetime) -> None:
            async with self.app.state.session_factory() as session:
                worker = await session.scalar(
                    select(RunnerWorkerRecord).where(
                        RunnerWorkerRecord.worker_key == "reliability-worker"
                    )
                )
                assert worker is not None
                worker.last_heartbeat_at = moment - timedelta(seconds=60)
                await session.commit()

        async def expire_dispatch_start(run_id: str, moment: datetime) -> None:
            async with self.app.state.session_factory() as session:
                run = await session.get(RunRecord, UUID(run_id))
                assert run is not None
                run.dispatched_at = moment - timedelta(seconds=301)
                await session.commit()

        def create_run(key: str, case_code: str) -> dict[str, object]:
            response = self.client.post(
                "/api/v1/runs",
                headers={**self.headers, "Idempotency-Key": key},
                json=self._run_payload(resources, case_code),
            )
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["timeout_seconds"], 60)
            return response.json()

        first = create_run("reliability-timeout", "TC-LOGIN-001")
        self.assertEqual(asyncio.run(dispatch()).published, 1)
        for status_value in ("PREPARING", "RUNNING"):
            status_response = self.client.post(
                f"/api/v1/internal/runs/{first['id']}/status",
                headers=RUNNER_HEADERS,
                json={
                    "status": status_value,
                    "worker_key": "reliability-worker",
                },
            )
            self.assertEqual(status_response.status_code, 200, status_response.text)
        running = self.client.get(f"/api/v1/runs/{first['id']}", headers=self.headers)
        self.assertTrue(running.json()["timeout_at"].endswith(("Z", "+00:00")))
        moment = datetime.now(UTC)
        asyncio.run(expire_run_timeout(str(first["id"]), moment))
        timed_out = asyncio.run(process_run_reliability(self.app.state.session_factory, now=moment))
        self.assertEqual((timed_out.timed_out, timed_out.leases_released), (1, 1))
        recovered = self.client.get(f"/api/v1/runs/{first['id']}", headers=self.headers)
        self.assertEqual(recovered.json()["status"], "TIMED_OUT")
        self.assertTrue(recovered.json()["cancel_requested"])

        self.assertEqual(
            self.client.put(
                heartbeat_url,
                headers=RUNNER_HEADERS,
                json=heartbeat_payload,
            ).status_code,
            200,
        )
        second = create_run("reliability-runner-lost", "TC-LOGIN-003")
        self.assertGreaterEqual(asyncio.run(dispatch()).published, 1)
        preparing = self.client.post(
            f"/api/v1/internal/runs/{second['id']}/status",
            headers=RUNNER_HEADERS,
            json={"status": "PREPARING", "worker_key": "reliability-worker"},
        )
        self.assertEqual(preparing.status_code, 200, preparing.text)
        moment = datetime.now(UTC)
        asyncio.run(expire_worker_heartbeat(moment))
        lost = asyncio.run(process_run_reliability(self.app.state.session_factory, now=moment))
        self.assertEqual((lost.runner_lost, lost.leases_released), (1, 1))
        recovered = self.client.get(f"/api/v1/runs/{second['id']}", headers=self.headers)
        self.assertEqual(recovered.json()["status"], "INFRA_ERROR")
        self.assertIn("heartbeat expired", recovered.json()["error_message"])

        self.assertEqual(
            self.client.put(
                heartbeat_url,
                headers=RUNNER_HEADERS,
                json=heartbeat_payload,
            ).status_code,
            200,
        )
        third = create_run("reliability-dispatch-stalled", "TC-LOGIN-007")
        self.assertGreaterEqual(asyncio.run(dispatch()).published, 1)
        moment = datetime.now(UTC)
        asyncio.run(expire_dispatch_start(str(third["id"]), moment))
        stalled = asyncio.run(
            process_run_reliability(
                self.app.state.session_factory,
                now=moment,
                dispatch_start_timeout_seconds=300,
            )
        )
        self.assertEqual((stalled.dispatch_stalled, stalled.leases_released), (1, 1))
        recovered = self.client.get(f"/api/v1/runs/{third['id']}", headers=self.headers)
        self.assertEqual(recovered.json()["status"], "INFRA_ERROR")

        async def recovery_evidence() -> tuple[int, int]:
            async with self.app.state.session_factory() as session:
                expired_leases = await session.scalar(
                    select(func.count())
                    .select_from(RunnerSlotLeaseRecord)
                    .where(RunnerSlotLeaseRecord.status == "EXPIRED")
                )
                audits = await session.scalar(
                    select(func.count())
                    .select_from(AuditLogRecord)
                    .where(
                        AuditLogRecord.action.in_(
                            ("run.timed_out", "run.runner_lost", "run.dispatch_stalled")
                        )
                    )
                )
                return int(expired_leases or 0), int(audits or 0)

        self.assertEqual(asyncio.run(recovery_evidence()), (3, 3))

    def test_regression_schedules_trigger_idempotently_and_apply_misfire_policy(self) -> None:
        resources = self._bootstrap()
        schedule_payload = {
            "key": "weekday-login",
            "name": "工作日登录回归",
            "target_id": resources["target_id"],
            "environment_id": resources["environment_id"],
            "baseline_id": resources["baseline_id"],
            "automation_package_id": resources["package_id"],
            "case_codes": ["TC-LOGIN-001"],
            "cron_expression": "0 9 * * 1-5",
            "timezone": "Asia/Shanghai",
            "misfire_policy": "FIRE_ONCE",
            "misfire_grace_seconds": 60,
        }
        created = self.client.post(
            f"/api/v1/projects/{resources['project_id']}/regression-schedules",
            headers=self.headers,
            json=schedule_payload,
        )
        self.assertEqual(created.status_code, 201, created.text)
        schedule = created.json()
        self.assertEqual(schedule["cron_expression"], "0 9 * * 1-5")
        self.assertEqual(schedule["timezone"], "Asia/Shanghai")
        self.assertIsNotNone(schedule["next_fire_at"])
        self.assertTrue(schedule["next_fire_at"].endswith(("Z", "+00:00")))

        listed = self.client.get(
            f"/api/v1/projects/{resources['project_id']}/regression-schedules",
            headers=self.headers,
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([item["id"] for item in listed.json()], [schedule["id"]])

        trigger_url = (
            f"/api/v1/projects/{resources['project_id']}/regression-schedules/"
            f"{schedule['id']}/trigger"
        )
        trigger_headers = {**self.headers, "Idempotency-Key": "manual-schedule-trigger-0001"}
        manual = self.client.post(trigger_url, headers=trigger_headers)
        self.assertEqual(manual.status_code, 201, manual.text)
        self.assertEqual(manual.json()["regression_schedule_id"], schedule["id"])
        self.assertIsNotNone(manual.json()["scheduled_for"])
        self.assertTrue(manual.json()["scheduled_for"].endswith(("Z", "+00:00")))
        replay = self.client.post(trigger_url, headers=trigger_headers)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["id"], manual.json()["id"])
        replayed_schedules = self.client.get(
            f"/api/v1/projects/{resources['project_id']}/regression-schedules",
            headers=self.headers,
        )
        self.assertEqual(
            replayed_schedules.json()[0]["last_scheduled_for"],
            manual.json()["scheduled_for"],
        )

        moment = datetime.now(UTC)
        missed_for = moment - timedelta(minutes=5)

        async def set_due(schedule_id: str, scheduled_for: datetime) -> None:
            async with self.app.state.session_factory() as session:
                record = await session.get(RegressionScheduleRecord, UUID(schedule_id))
                assert record is not None
                record.next_fire_at = scheduled_for
                await session.commit()

        asyncio.run(set_due(schedule["id"], missed_for))
        dispatched = asyncio.run(process_due_schedules(self.app.state.session_factory, now=moment))
        self.assertEqual(
            (
                dispatched.selected,
                dispatched.triggered,
                dispatched.skipped,
                dispatched.blocked,
                dispatched.failed,
            ),
            (1, 1, 0, 0, 0),
        )
        firings = self.client.get(
            f"/api/v1/projects/{resources['project_id']}/regression-schedules/"
            f"{schedule['id']}/firings",
            headers=self.headers,
        )
        self.assertEqual(firings.status_code, 200, firings.text)
        self.assertEqual(
            {item["trigger_kind"] for item in firings.json()},
            {"MANUAL", "MISFIRE"},
        )
        scheduled_firing = next(
            item for item in firings.json() if item["trigger_kind"] == "MISFIRE"
        )
        self.assertEqual(scheduled_firing["status"], "TRIGGERED")
        self.assertIsNotNone(scheduled_firing["run_id"])
        self.assertTrue(scheduled_firing["scheduled_for"].endswith(("Z", "+00:00")))

        skipped_payload = {
            **schedule_payload,
            "key": "skip-stale-login",
            "name": "跳过过期登录回归",
            "misfire_policy": "SKIP",
        }
        skipped_schedule = self.client.post(
            f"/api/v1/projects/{resources['project_id']}/regression-schedules",
            headers=self.headers,
            json=skipped_payload,
        )
        self.assertEqual(skipped_schedule.status_code, 201, skipped_schedule.text)
        scoped_manual = self.client.post(
            f"/api/v1/projects/{resources['project_id']}/regression-schedules/"
            f"{skipped_schedule.json()['id']}/trigger",
            headers=trigger_headers,
        )
        self.assertEqual(scoped_manual.status_code, 201, scoped_manual.text)
        self.assertNotEqual(scoped_manual.json()["id"], manual.json()["id"])
        self.assertEqual(
            scoped_manual.json()["regression_schedule_id"],
            skipped_schedule.json()["id"],
        )
        asyncio.run(set_due(skipped_schedule.json()["id"], missed_for))
        skipped = asyncio.run(process_due_schedules(self.app.state.session_factory, now=moment))
        self.assertEqual((skipped.selected, skipped.skipped, skipped.failed), (1, 1, 0))

        paused = self.client.patch(
            f"/api/v1/projects/{resources['project_id']}/regression-schedules/{schedule['id']}",
            headers=self.headers,
            json={"status": "PAUSED"},
        )
        self.assertEqual(paused.status_code, 200, paused.text)
        self.assertEqual(paused.json()["status"], "PAUSED")
        self.assertIsNone(paused.json()["next_fire_at"])

        async def count_firings() -> int:
            async with self.app.state.session_factory() as session:
                count = await session.scalar(
                    select(func.count()).select_from(RegressionScheduleFiringRecord)
                )
                return int(count or 0)

        self.assertEqual(asyncio.run(count_firings()), 4)

    def test_metrics_expose_dispatch_and_schedule_backlog(self) -> None:
        resources = self._bootstrap()
        run = self.client.post(
            "/api/v1/runs",
            headers={**self.headers, "Idempotency-Key": "metrics-backlog-run"},
            json=self._run_payload(resources, "TC-LOGIN-001"),
        )
        self.assertEqual(run.status_code, 201, run.text)
        schedule = self.client.post(
            f"/api/v1/projects/{resources['project_id']}/regression-schedules",
            headers=self.headers,
            json={
                "key": "metrics-schedule",
                "name": "Metrics Schedule",
                "target_id": resources["target_id"],
                "environment_id": resources["environment_id"],
                "baseline_id": resources["baseline_id"],
                "automation_package_id": resources["package_id"],
                "cron_expression": "*/5 * * * *",
                "timezone": "UTC",
            },
        )
        self.assertEqual(schedule.status_code, 201, schedule.text)
        moment = datetime.now(UTC)

        async def age_backlogs() -> None:
            async with self.app.state.session_factory() as session:
                run_record = await session.get(RunRecord, UUID(run.json()["id"]))
                schedule_record = await session.get(
                    RegressionScheduleRecord,
                    UUID(schedule.json()["id"]),
                )
                assert run_record is not None and schedule_record is not None
                run_record.dispatch_state = "WAITING"
                run_record.dispatch_wait_reason = "NO_HEALTHY_RUNNER"
                run_record.created_at = moment - timedelta(minutes=10)
                schedule_record.next_fire_at = moment - timedelta(minutes=5)
                operator_id = uuid4()
                webhook_config = QualityWebhookConfigRecord(
                    id=uuid4(),
                    project_id=UUID(resources["project_id"]),
                    enabled=True,
                    endpoint_url="https://metrics.example.invalid/receiver",
                    minimum_alert_status="WARNING",
                    cooldown_seconds=3600,
                    next_evaluation_at=moment - timedelta(minutes=12),
                    silenced_until=moment + timedelta(hours=1),
                    silenced_by=operator_id,
                    silence_reason="metrics fixture",
                )
                session.add(webhook_config)
                await session.flush()
                failed_delivery = QualityWebhookDeliveryRecord(
                    id=uuid4(),
                    project_id=UUID(resources["project_id"]),
                    webhook_config_id=webhook_config.id,
                    event_type="quality.alert.triggered",
                    dedupe_key=f"metrics-failed:{uuid4()}",
                    destination_display="https://metrics.example.invalid/***",
                    payload={"event_type": "quality.alert.triggered"},
                    status="FAILED",
                    attempts=1,
                    available_at=moment - timedelta(minutes=20),
                    response_status=400,
                    last_error="Webhook endpoint returned HTTP 400",
                    created_at=moment - timedelta(minutes=20),
                )
                session.add(failed_delivery)
                await session.flush()
                session.add(
                    QualityWebhookDeliveryRecord(
                        id=uuid4(),
                        project_id=UUID(resources["project_id"]),
                        webhook_config_id=webhook_config.id,
                        event_type="quality.alert.triggered",
                        dedupe_key=f"metrics-replay:{uuid4()}",
                        destination_display="https://metrics.example.invalid/***",
                        payload={"event_type": "quality.alert.triggered"},
                        status="PENDING",
                        attempts=0,
                        available_at=moment - timedelta(minutes=15),
                        replay_of_id=failed_delivery.id,
                        replayed_by=operator_id,
                        replay_reason="metrics fixture replay",
                        created_at=moment - timedelta(minutes=15),
                    )
                )
                await session.commit()

        asyncio.run(age_backlogs())
        metrics = self.client.get("/metrics")
        self.assertEqual(metrics.status_code, 200, metrics.text)
        self.assertIn('testops_runs_in_flight{status="QUEUED"} 1.0', metrics.text)
        self.assertIn("testops_dispatch_waiting_runs 1.0", metrics.text)
        self.assertIn("testops_schedule_due_backlog 1.0", metrics.text)
        self.assertIn("testops_reliability_snapshot_success 1.0", metrics.text)
        self.assertIn("testops_quality_alert_evaluation_due_configs 1.0", metrics.text)
        self.assertIn("testops_quality_alert_active_silences 1.0", metrics.text)
        self.assertIn(
            'testops_quality_webhook_deliveries{status="PENDING"} 1.0',
            metrics.text,
        )
        self.assertIn(
            'testops_quality_webhook_deliveries{status="FAILED"} 1.0',
            metrics.text,
        )
        self.assertIn("testops_quality_webhook_replay_deliveries 1.0", metrics.text)
        self.assertIn("testops_quality_operations_snapshot_success 1.0", metrics.text)

    def test_project_quality_policy_trends_and_failure_clusters(self) -> None:
        resources = self._bootstrap()
        policy_url = f"/api/v1/projects/{resources['project_id']}/quality-policy"
        initial_policy = self.client.get(policy_url, headers=self.headers)
        self.assertEqual(initial_policy.status_code, 200, initial_policy.text)
        self.assertEqual(initial_policy.json()["target_pass_rate_percent"], 95)
        self.assertEqual(initial_policy.json()["window_days"], 30)
        self.assertEqual(initial_policy.json()["alert_warning_drop_percentage_points"], 5)
        self.assertEqual(initial_policy.json()["alert_critical_drop_percentage_points"], 10)

        invalid_policy = self.client.patch(
            policy_url,
            headers=self.headers,
            json={"target_pass_rate_percent": 0},
        )
        self.assertEqual(invalid_policy.status_code, 422, invalid_policy.text)
        policy = self.client.patch(
            policy_url,
            headers=self.headers,
            json={
                "target_pass_rate_percent": 80,
                "window_days": 14,
                "alert_warning_drop_percentage_points": 7,
                "alert_critical_drop_percentage_points": 15,
            },
        )
        self.assertEqual(policy.status_code, 200, policy.text)
        self.assertEqual(policy.json()["target_pass_rate_percent"], 80)
        self.assertEqual(policy.json()["window_days"], 14)
        self.assertEqual(policy.json()["alert_warning_drop_percentage_points"], 7)
        self.assertEqual(policy.json()["alert_critical_drop_percentage_points"], 15)
        invalid_alert_order = self.client.patch(
            policy_url,
            headers=self.headers,
            json={"alert_warning_drop_percentage_points": 15},
        )
        self.assertEqual(invalid_alert_order.status_code, 422, invalid_alert_order.text)

        statuses = ("PASSED", "FAILED", "FAILED", "INFRA_ERROR", "TIMED_OUT", "CANCELED")
        run_ids: list[UUID] = []
        for index, _status in enumerate(statuses):
            created = self.client.post(
                "/api/v1/runs",
                headers={**self.headers, "Idempotency-Key": f"quality-run-{index:04d}"},
                json=self._run_payload(resources, "TC-LOGIN-001"),
            )
            self.assertEqual(created.status_code, 201, created.text)
            run_ids.append(UUID(created.json()["id"]))

        previous_run_ids: list[UUID] = []
        for index in range(3):
            created = self.client.post(
                "/api/v1/runs",
                headers={
                    **self.headers,
                    "Idempotency-Key": f"quality-previous-run-{index:04d}",
                },
                json=self._run_payload(resources, "TC-LOGIN-001"),
            )
            self.assertEqual(created.status_code, 201, created.text)
            previous_run_ids.append(UUID(created.json()["id"]))

        moment = datetime.now(UTC)
        assertion_messages = (
            "Expected 200 at https://one.example.invalid/orders request "
            "11111111-1111-4111-8111-111111111111 attempt 1 token=alpha",
            "Expected 404 at https://two.example.invalid/orders request "
            "22222222-2222-4222-8222-222222222222 attempt 2 token=beta",
        )

        async def seed_quality_history() -> None:
            async with self.app.state.session_factory() as session:
                for index, (run_id, run_status) in enumerate(zip(run_ids, statuses, strict=True)):
                    run = await session.get(RunRecord, run_id)
                    run_case = await session.scalar(
                        select(RunCaseRecord).where(RunCaseRecord.run_id == run_id)
                    )
                    assert run is not None and run_case is not None
                    finished_at = moment - timedelta(days=1 if index < 2 else 0, minutes=index)
                    run.status = run_status
                    run.started_at = finished_at - timedelta(seconds=2)
                    run.finished_at = finished_at
                    if run_status == "PASSED":
                        run_case.status = "PASSED"
                    elif run_status == "FAILED":
                        run_case.status = "FAILED"
                        run_case.failure_category = "ASSERTION"
                        run_case.error_message = assertion_messages[index - 1]
                    elif run_status == "INFRA_ERROR":
                        run_case.status = "INFRA_ERROR"
                        run_case.failure_category = "BROWSER"
                        run_case.error_message = "Chromium process exited with code 137"
                    elif run_status == "TIMED_OUT":
                        run_case.status = "TIMED_OUT"
                        run_case.error_message = "Run timed out after 3600 seconds"
                    else:
                        run_case.status = "CANCELED"
                for index, run_id in enumerate(previous_run_ids):
                    run = await session.get(RunRecord, run_id)
                    run_case = await session.scalar(
                        select(RunCaseRecord).where(RunCaseRecord.run_id == run_id)
                    )
                    assert run is not None and run_case is not None
                    finished_at = moment - timedelta(days=20, minutes=index)
                    run.status = "PASSED"
                    run.started_at = finished_at - timedelta(seconds=2)
                    run.finished_at = finished_at
                    run_case.status = "PASSED"
                await session.commit()

        asyncio.run(seed_quality_history())
        analytics_url = f"/api/v1/projects/{resources['project_id']}/quality/analytics"
        analytics = self.client.get(analytics_url, headers=self.headers)
        self.assertEqual(analytics.status_code, 200, analytics.text)
        payload = analytics.json()
        self.assertEqual(payload["window_days"], 14)
        self.assertEqual(
            payload["filters"],
            {"target_id": None, "environment_id": None, "baseline_id": None},
        )
        self.assertEqual(payload["target_pass_rate_percent"], 80)
        self.assertEqual(payload["slo_status"], "BREACHED")
        self.assertEqual(payload["comparison"]["alert_status"], "CRITICAL")
        self.assertEqual(payload["comparison"]["warning_drop_percentage_points"], 7)
        self.assertEqual(payload["comparison"]["critical_drop_percentage_points"], 15)
        self.assertEqual(
            payload["comparison"]["previous_window_ended_at"],
            payload["window_started_at"],
        )
        comparison_signals = {
            signal["metric"]: signal for signal in payload["comparison"]["signals"]
        }
        self.assertEqual(comparison_signals["RUN_PASS_RATE"]["previous_percent"], 100.0)
        self.assertEqual(comparison_signals["RUN_PASS_RATE"]["current_percent"], 33.33)
        self.assertEqual(
            comparison_signals["RUN_PASS_RATE"]["delta_percentage_points"],
            -66.67,
        )
        self.assertEqual(comparison_signals["EXECUTION_RELIABILITY"]["current_percent"], 60.0)
        self.assertEqual(payload["runs"]["total_terminal_runs"], 6)
        self.assertEqual(payload["runs"]["conclusive_runs"], 3)
        self.assertEqual(payload["runs"]["pass_rate_percent"], 33.33)
        self.assertEqual(payload["runs"]["execution_reliability_percent"], 60.0)
        self.assertEqual(payload["cases"]["total_terminal_cases"], 6)
        self.assertEqual(payload["cases"]["timed_out_cases"], 1)
        self.assertEqual(payload["cases"]["canceled_cases"], 1)
        self.assertEqual(len(payload["trend"]), 14)
        self.assertTrue(payload["trend"][0]["bucket_started_at"].endswith("Z"))
        self.assertFalse(payload["failure_data_truncated"])

        assertion_cluster = payload["failure_clusters"][0]
        self.assertEqual(assertion_cluster["failure_category"], "ASSERTION")
        self.assertEqual(assertion_cluster["occurrences"], 2)
        self.assertEqual(assertion_cluster["affected_runs"], 2)
        self.assertEqual(assertion_cluster["failed_occurrences"], 2)
        self.assertIn("<url>", assertion_cluster["message_pattern"])
        self.assertIn("<uuid>", assertion_cluster["message_pattern"])
        self.assertIn("token=<redacted>", assertion_cluster["message_pattern"])
        self.assertNotIn("alpha", assertion_cluster["message_pattern"])
        self.assertNotIn("two.example.invalid", assertion_cluster["message_pattern"])
        flaky = payload["flaky"]
        self.assertEqual(flaky["minimum_conclusive_executions"], 3)
        self.assertEqual(flaky["minimum_status_transitions"], 2)
        self.assertEqual(flaky["analyzed_executions"], 3)
        self.assertEqual(flaky["detected_cases"], 1)
        self.assertFalse(flaky["data_truncated"])
        self.assertEqual(len(flaky["cases"]), 1)
        flaky_case = flaky["cases"][0]
        self.assertEqual(flaky_case["case_code"], "TC-LOGIN-001")
        self.assertEqual(flaky_case["conclusive_executions"], 3)
        self.assertEqual(flaky_case["passed_executions"], 1)
        self.assertEqual(flaky_case["failed_executions"], 2)
        self.assertEqual(flaky_case["pass_rate_percent"], 33.33)
        self.assertEqual(flaky_case["status_transitions"], 2)
        self.assertEqual(flaky_case["transition_rate_percent"], 100.0)
        self.assertEqual(flaky_case["latest_status"], "FAILED")

        seven_days = self.client.get(f"{analytics_url}?window_days=7", headers=self.headers)
        self.assertEqual(seven_days.status_code, 200, seven_days.text)
        self.assertEqual(seven_days.json()["window_days"], 7)
        self.assertEqual(len(seven_days.json()["trend"]), 7)
        invalid_window = self.client.get(
            f"{analytics_url}?window_days=0",
            headers=self.headers,
        )
        self.assertEqual(invalid_window.status_code, 422, invalid_window.text)

        second_target = self.client.post(
            f"/api/v1/projects/{resources['project_id']}/targets",
            headers=self.headers,
            json={
                "key": "admin-web",
                "name": "第二 Web 管理端",
                "target_type": "WEB",
                "browser": "chromium",
            },
        )
        self.assertEqual(second_target.status_code, 201, second_target.text)
        second_target_id = second_target.json()["id"]
        second_environment = self.client.post(
            (f"/api/v1/projects/{resources['project_id']}/targets/{second_target_id}/environments"),
            headers=self.headers,
            json={
                "key": "staging",
                "name": "第二测试环境",
                "web_config": {
                    "base_url": "https://second.example.invalid/login",
                    "headless": True,
                    "capture_trace": True,
                },
            },
        )
        self.assertEqual(second_environment.status_code, 201, second_environment.text)
        second_environment_id = second_environment.json()["id"]

        earlier_baseline_directory = ROOT / "baselines/yanjia-ai-web/case-v1.0.0"
        earlier_baseline_document = json.loads(
            (earlier_baseline_directory / "case-baseline.json").read_text("utf-8")
        )
        earlier_manifest = json.loads(
            (earlier_baseline_directory / "manifest.json").read_text("utf-8")
        )
        second_baseline = self.client.post(
            f"/api/v1/projects/{resources['project_id']}/baselines",
            headers=self.headers,
            json={
                "baseline": earlier_baseline_document,
                "digest": earlier_manifest["baseline"]["digest"],
            },
        )
        self.assertEqual(second_baseline.status_code, 201, second_baseline.text)
        second_baseline_id = second_baseline.json()["baseline_id"]
        second_package = self.client.post(
            (
                f"/api/v1/projects/{resources['project_id']}/targets/"
                f"{second_target_id}/automation-packages"
            ),
            headers=self.headers,
            json={"name": "yanjia-admin-web", "version": "0.1.0", "digest": PACKAGE_DIGEST},
        )
        self.assertEqual(second_package.status_code, 201, second_package.text)
        second_resources = {
            "project_id": resources["project_id"],
            "target_id": second_target_id,
            "environment_id": second_environment_id,
            "baseline_id": second_baseline_id,
            "package_id": second_package.json()["id"],
        }
        second_run = self.client.post(
            "/api/v1/runs",
            headers={**self.headers, "Idempotency-Key": "quality-dimension-run-0001"},
            json=self._run_payload(second_resources, "TC-LOGIN-001"),
        )
        self.assertEqual(second_run.status_code, 201, second_run.text)

        async def seed_second_dimension() -> None:
            async with self.app.state.session_factory() as session:
                run = await session.get(RunRecord, UUID(second_run.json()["id"]))
                run_case = await session.scalar(
                    select(RunCaseRecord).where(
                        RunCaseRecord.run_id == UUID(second_run.json()["id"])
                    )
                )
                assert run is not None and run_case is not None
                run.status = "PASSED"
                run.started_at = moment - timedelta(seconds=2)
                run.finished_at = moment
                run_case.status = "PASSED"
                await session.commit()

        asyncio.run(seed_second_dimension())

        dimension_queries = (
            ("target_id", second_target_id),
            ("environment_id", second_environment_id),
            ("baseline_id", second_baseline_id),
        )
        for query_name, query_value in dimension_queries:
            filtered = self.client.get(
                f"{analytics_url}?{query_name}={query_value}",
                headers=self.headers,
            )
            self.assertEqual(filtered.status_code, 200, filtered.text)
            filtered_payload = filtered.json()
            self.assertEqual(filtered_payload["filters"][query_name], query_value)
            self.assertEqual(filtered_payload["runs"]["total_terminal_runs"], 1)
            self.assertEqual(filtered_payload["runs"]["pass_rate_percent"], 100.0)
            self.assertEqual(filtered_payload["slo_status"], "MET")
            self.assertEqual(filtered_payload["flaky"]["detected_cases"], 0)

        combined = self.client.get(
            (
                f"{analytics_url}?target_id={second_target_id}"
                f"&environment_id={second_environment_id}&baseline_id={second_baseline_id}"
            ),
            headers=self.headers,
        )
        self.assertEqual(combined.status_code, 200, combined.text)
        self.assertEqual(combined.json()["runs"]["total_terminal_runs"], 1)

        no_data = self.client.get(
            (
                f"{analytics_url}?target_id={resources['target_id']}"
                f"&baseline_id={second_baseline_id}"
            ),
            headers=self.headers,
        )
        self.assertEqual(no_data.status_code, 200, no_data.text)
        self.assertEqual(no_data.json()["slo_status"], "NO_DATA")
        self.assertEqual(no_data.json()["runs"]["total_terminal_runs"], 0)

        mismatched_environment = self.client.get(
            (
                f"{analytics_url}?target_id={second_target_id}"
                f"&environment_id={resources['environment_id']}"
            ),
            headers=self.headers,
        )
        self.assertEqual(mismatched_environment.status_code, 422, mismatched_environment.text)
        missing_target = self.client.get(
            f"{analytics_url}?target_id={uuid4()}",
            headers=self.headers,
        )
        self.assertEqual(missing_target.status_code, 404, missing_target.text)

        async def policy_audit_count() -> int:
            async with self.app.state.session_factory() as session:
                count = await session.scalar(
                    select(func.count())
                    .select_from(AuditLogRecord)
                    .where(AuditLogRecord.action == "project.quality_policy_updated")
                )
                return int(count or 0)

        self.assertEqual(asyncio.run(policy_audit_count()), 1)

    def test_quality_webhook_configuration_test_delivery_and_retry(self) -> None:
        resources = self._bootstrap()
        project_id = resources["project_id"]
        config_url = f"/api/v1/projects/{project_id}/quality/webhook"
        deliveries_url = f"{config_url}/deliveries"

        initial = self.client.get(config_url, headers=self.headers)
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertEqual(
            initial.json(),
            {
                "project_id": project_id,
                "enabled": False,
                "endpoint_configured": False,
                "endpoint_display": None,
                "minimum_alert_status": "WARNING",
                "cooldown_seconds": 3600,
                "signing_configured": False,
                "last_evaluated_at": None,
                "next_evaluation_at": None,
                "silenced_until": None,
                "silenced_by": None,
                "silenced_by_display_name": None,
                "silence_reason": None,
                "updated_at": None,
            },
        )
        unavailable_test = self.client.post(f"{config_url}/test", headers=self.headers)
        self.assertEqual(unavailable_test.status_code, 409, unavailable_test.text)
        unavailable_silence = self.client.put(
            f"{config_url}/silence",
            headers=self.headers,
            json={
                "silenced_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "reason": "maintenance",
            },
        )
        self.assertEqual(unavailable_silence.status_code, 409, unavailable_silence.text)
        insecure = self.client.patch(
            config_url,
            headers=self.headers,
            json={"endpoint_url": "http://127.0.0.1/internal", "enabled": True},
        )
        self.assertEqual(insecure.status_code, 422, insecure.text)

        endpoint = "https://hooks.example.invalid/private-token"
        configured = self.client.patch(
            config_url,
            headers=self.headers,
            json={
                "enabled": True,
                "endpoint_url": endpoint,
                "minimum_alert_status": "CRITICAL",
                "cooldown_seconds": 900,
                "signing_secret_name": "QUALITY_WEBHOOK_YANJIA",
                "signing_secret_ref": "secret://quality/yanjia/webhook-signing",
            },
        )
        self.assertEqual(configured.status_code, 200, configured.text)
        configured_payload = configured.json()
        self.assertTrue(configured_payload["enabled"])
        self.assertEqual(
            configured_payload["endpoint_display"],
            "https://hooks.example.invalid/***",
        )
        self.assertEqual(configured_payload["minimum_alert_status"], "CRITICAL")
        self.assertEqual(configured_payload["cooldown_seconds"], 900)
        self.assertIsNotNone(configured_payload["next_evaluation_at"])
        self.assertTrue(configured_payload["signing_configured"])
        self.assertNotIn("private-token", configured.text)
        self.assertNotIn("webhook-signing", configured.text)
        for invalid_silence_until in (
            datetime.now(UTC) - timedelta(seconds=1),
            datetime.now(UTC) + timedelta(days=31),
        ):
            invalid_silence = self.client.put(
                f"{config_url}/silence",
                headers=self.headers,
                json={
                    "silenced_until": invalid_silence_until.isoformat(),
                    "reason": "invalid window",
                },
            )
            self.assertEqual(invalid_silence.status_code, 422, invalid_silence.text)

        queued = self.client.post(f"{config_url}/test", headers=self.headers)
        self.assertEqual(queued.status_code, 202, queued.text)
        self.assertEqual(queued.json()["status"], "PENDING")
        self.assertEqual(queued.json()["attempts"], 0)

        sender = RecordingQualityWebhookSender(204)
        delivered_summary = asyncio.run(
            dispatch_quality_webhook_batch(self.app.state.session_factory, sender)
        )
        self.assertEqual(
            (
                delivered_summary.selected,
                delivered_summary.delivered,
                delivered_summary.retrying,
                delivered_summary.failed,
            ),
            (1, 1, 0, 0),
        )
        self.assertEqual(
            sender.calls,
            [(endpoint, "quality.alert.test", "QUALITY_WEBHOOK_YANJIA")],
        )
        delivered = self.client.get(deliveries_url, headers=self.headers)
        self.assertEqual(delivered.status_code, 200, delivered.text)
        self.assertEqual(delivered.json()[0]["status"], "DELIVERED")
        self.assertEqual(delivered.json()[0]["attempts"], 1)
        self.assertEqual(delivered.json()[0]["response_status"], 204)
        self.assertIsNotNone(delivered.json()[0]["delivered_at"])

        queued_retry = self.client.post(f"{config_url}/test", headers=self.headers)
        self.assertEqual(queued_retry.status_code, 202, queued_retry.text)
        retry_moment = datetime.now(UTC)
        retry_summary = asyncio.run(
            dispatch_quality_webhook_batch(
                self.app.state.session_factory,
                RecordingQualityWebhookSender(503),
                now=retry_moment,
            )
        )
        self.assertEqual(
            (retry_summary.selected, retry_summary.retrying, retry_summary.failed),
            (1, 1, 0),
        )
        failed_summary = asyncio.run(
            dispatch_quality_webhook_batch(
                self.app.state.session_factory,
                RecordingQualityWebhookSender(400),
                now=retry_moment + timedelta(seconds=10),
            )
        )
        self.assertEqual(
            (failed_summary.selected, failed_summary.retrying, failed_summary.failed),
            (1, 0, 1),
        )
        history = self.client.get(f"{deliveries_url}?limit=1", headers=self.headers)
        self.assertEqual(history.status_code, 200, history.text)
        failed_delivery = history.json()[0]
        self.assertEqual(failed_delivery["status"], "FAILED")
        self.assertEqual(failed_delivery["attempts"], 2)
        self.assertEqual(failed_delivery["response_status"], 400)
        self.assertEqual(failed_delivery["last_error"], "Webhook endpoint returned HTTP 400")
        self.assertIsNone(failed_delivery["replay_of_id"])

        replay_delivered = self.client.post(
            f"{deliveries_url}/{queued.json()['id']}/replay",
            headers=self.headers,
            json={"reason": "should not replay a delivered event"},
        )
        self.assertEqual(replay_delivered.status_code, 409, replay_delivered.text)
        invalid_replay = self.client.post(
            f"{deliveries_url}/{failed_delivery['id']}/replay",
            headers=self.headers,
            json={"reason": " "},
        )
        self.assertEqual(invalid_replay.status_code, 422, invalid_replay.text)

        recovered_endpoint = "https://recovered.example.invalid/new-token"
        reconfigured = self.client.patch(
            config_url,
            headers=self.headers,
            json={"endpoint_url": recovered_endpoint},
        )
        self.assertEqual(reconfigured.status_code, 200, reconfigured.text)
        replayed = self.client.post(
            f"{deliveries_url}/{failed_delivery['id']}/replay",
            headers=self.headers,
            json={"reason": "receiver contract fixed"},
        )
        self.assertEqual(replayed.status_code, 202, replayed.text)
        replayed_payload = replayed.json()
        self.assertEqual(replayed_payload["status"], "PENDING")
        self.assertEqual(replayed_payload["attempts"], 0)
        self.assertIsNone(replayed_payload["response_status"])
        self.assertEqual(replayed_payload["replay_of_id"], failed_delivery["id"])
        self.assertEqual(replayed_payload["replayed_by_display_name"], "Integration Admin")
        self.assertEqual(replayed_payload["replay_reason"], "receiver contract fixed")
        self.assertEqual(
            replayed_payload["destination_display"],
            "https://recovered.example.invalid/***",
        )

        duplicate_replay = self.client.post(
            f"{deliveries_url}/{failed_delivery['id']}/replay",
            headers=self.headers,
            json={"reason": "duplicate operator action"},
        )
        self.assertEqual(duplicate_replay.status_code, 409, duplicate_replay.text)
        replay_pending = self.client.post(
            f"{deliveries_url}/{replayed_payload['id']}/replay",
            headers=self.headers,
            json={"reason": "pending records cannot be replayed"},
        )
        self.assertEqual(replay_pending.status_code, 409, replay_pending.text)

        replay_sender = RecordingQualityWebhookSender(204)
        replay_summary = asyncio.run(
            dispatch_quality_webhook_batch(self.app.state.session_factory, replay_sender)
        )
        self.assertEqual(
            (replay_summary.selected, replay_summary.delivered, replay_summary.failed),
            (1, 1, 0),
        )
        self.assertEqual(
            replay_sender.calls,
            [(recovered_endpoint, "quality.alert.test", "QUALITY_WEBHOOK_YANJIA")],
        )
        replay_history = self.client.get(f"{deliveries_url}?limit=3", headers=self.headers)
        self.assertEqual(replay_history.status_code, 200, replay_history.text)
        replay_rows = replay_history.json()
        self.assertEqual(replay_rows[0]["id"], replayed_payload["id"])
        self.assertEqual(replay_rows[0]["status"], "DELIVERED")
        self.assertEqual(replay_rows[1]["id"], failed_delivery["id"])
        self.assertEqual(replay_rows[1]["status"], "FAILED")

        cleared = self.client.patch(
            config_url,
            headers=self.headers,
            json={"clear_signing_secret": True},
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertFalse(cleared.json()["signing_configured"])

        async def inspect_sensitive_state() -> tuple[str, str, int, int, str, str]:
            async with self.app.state.session_factory() as session:
                config = await session.scalar(
                    select(QualityWebhookConfigRecord).where(
                        QualityWebhookConfigRecord.project_id == UUID(project_id)
                    )
                )
                audit_rows = tuple(
                    await session.scalars(
                        select(AuditLogRecord).where(
                            AuditLogRecord.project_id == UUID(project_id),
                            AuditLogRecord.action.in_(
                                (
                                    "project.quality_webhook_updated",
                                    "project.quality_webhook_test_queued",
                                    "project.quality_webhook_delivery_replayed",
                                )
                            ),
                        )
                    )
                )
                assert config is not None
                source = await session.get(
                    QualityWebhookDeliveryRecord,
                    UUID(failed_delivery["id"]),
                )
                replay = await session.get(
                    QualityWebhookDeliveryRecord,
                    UUID(replayed_payload["id"]),
                )
                assert source is not None
                assert replay is not None
                audit_json = json.dumps(
                    [row.details for row in audit_rows],
                    ensure_ascii=False,
                )
                return (
                    config.endpoint_url,
                    audit_json,
                    len(audit_rows),
                    len(
                        tuple(
                            await session.scalars(
                                select(QualityWebhookDeliveryRecord).where(
                                    QualityWebhookDeliveryRecord.project_id == UUID(project_id)
                                )
                            )
                        )
                    ),
                    str(source.payload["event_id"]),
                    str(replay.payload["event_id"]),
                )

        (
            stored_endpoint,
            audit_json,
            audit_count,
            delivery_count,
            source_event_id,
            replay_event_id,
        ) = asyncio.run(inspect_sensitive_state())
        self.assertEqual(stored_endpoint, recovered_endpoint)
        self.assertNotIn("private-token", audit_json)
        self.assertNotIn("new-token", audit_json)
        self.assertNotIn("webhook-signing", audit_json)
        self.assertEqual(audit_count, 6)
        self.assertEqual(delivery_count, 3)
        self.assertEqual(source_event_id, replay_event_id)

    def test_quality_alert_evaluator_transitions_cooldown_and_idempotency(self) -> None:
        resources = self._bootstrap()
        project_id = resources["project_id"]
        config_url = f"/api/v1/projects/{project_id}/quality/webhook"
        configured = self.client.patch(
            config_url,
            headers=self.headers,
            json={
                "enabled": True,
                "endpoint_url": "https://quality.example.invalid/receiver-token",
                "minimum_alert_status": "WARNING",
                "cooldown_seconds": 3600,
            },
        )
        self.assertEqual(configured.status_code, 200, configured.text)

        previous_run = self.client.post(
            "/api/v1/runs",
            headers={**self.headers, "Idempotency-Key": "quality-alert-previous-0001"},
            json=self._run_payload(resources, "TC-LOGIN-001"),
        )
        current_run = self.client.post(
            "/api/v1/runs",
            headers={**self.headers, "Idempotency-Key": "quality-alert-current-0001"},
            json=self._run_payload(resources, "TC-LOGIN-001"),
        )
        self.assertEqual(previous_run.status_code, 201, previous_run.text)
        self.assertEqual(current_run.status_code, 201, current_run.text)
        previous_run_id = UUID(previous_run.json()["id"])
        current_run_id = UUID(current_run.json()["id"])
        moment = datetime.now(UTC) + timedelta(seconds=5)

        async def seed_windows() -> None:
            async with self.app.state.session_factory() as session:
                previous = await session.get(RunRecord, previous_run_id)
                current = await session.get(RunRecord, current_run_id)
                previous_case = await session.scalar(
                    select(RunCaseRecord).where(RunCaseRecord.run_id == previous_run_id)
                )
                current_case = await session.scalar(
                    select(RunCaseRecord).where(RunCaseRecord.run_id == current_run_id)
                )
                assert previous is not None and current is not None
                assert previous_case is not None and current_case is not None
                previous.status = "PASSED"
                previous.started_at = moment - timedelta(days=40, seconds=1)
                previous.finished_at = moment - timedelta(days=40)
                previous_case.status = "PASSED"
                current.status = "FAILED"
                current.started_at = moment - timedelta(days=1, seconds=1)
                current.finished_at = moment - timedelta(days=1)
                current_case.status = "FAILED"
                await session.commit()

        async def set_current_status(status_value: str) -> None:
            async with self.app.state.session_factory() as session:
                current = await session.get(RunRecord, current_run_id)
                current_case = await session.scalar(
                    select(RunCaseRecord).where(RunCaseRecord.run_id == current_run_id)
                )
                assert current is not None and current_case is not None
                current.status = status_value
                current_case.status = status_value
                await session.commit()

        asyncio.run(seed_windows())
        first = asyncio.run(
            evaluate_quality_alert_batch(
                self.app.state.session_factory,
                now=moment,
                evaluation_interval_seconds=60,
            )
        )
        self.assertEqual(
            (first.selected, first.evaluated, first.transitions, first.queued),
            (1, 1, 3, 2),
        )
        states = self.client.get(f"{config_url}/states", headers=self.headers)
        self.assertEqual(states.status_code, 200, states.text)
        self.assertEqual(len(states.json()), 3)
        critical_states = [
            state for state in states.json() if state["current_status"] == "CRITICAL"
        ]
        self.assertEqual(len(critical_states), 2)
        self.assertTrue(
            all(state["active_notification_status"] == "CRITICAL" for state in critical_states)
        )
        first_history = self.client.get(
            f"{config_url}/deliveries",
            headers=self.headers,
        )
        self.assertEqual(first_history.status_code, 200, first_history.text)
        self.assertEqual(len(first_history.json()), 2)
        self.assertEqual(
            {item["event_type"] for item in first_history.json()},
            {"quality.alert.triggered"},
        )

        acknowledged_metric = critical_states[0]["metric"]
        acknowledgement_url = f"{config_url}/states/{acknowledged_metric}/acknowledgement"
        acknowledged = self.client.put(
            acknowledgement_url,
            headers=self.headers,
            json={"note": "Investigating the quality regression"},
        )
        self.assertEqual(acknowledged.status_code, 200, acknowledged.text)
        self.assertIsNotNone(acknowledged.json()["acknowledged_at"])
        self.assertEqual(
            acknowledged.json()["acknowledged_by_display_name"],
            "Integration Admin",
        )
        self.assertEqual(
            acknowledged.json()["acknowledgement_note"],
            "Investigating the quality regression",
        )
        cleared_acknowledgement = self.client.delete(
            acknowledgement_url,
            headers=self.headers,
        )
        self.assertEqual(
            cleared_acknowledgement.status_code,
            200,
            cleared_acknowledgement.text,
        )
        self.assertIsNone(cleared_acknowledgement.json()["acknowledged_at"])
        acknowledged_again = self.client.put(
            acknowledgement_url,
            headers=self.headers,
            json={"note": "Keep this acknowledged until the signal changes"},
        )
        self.assertEqual(acknowledged_again.status_code, 200, acknowledged_again.text)

        stable_state = next(state for state in states.json() if state["current_status"] == "STABLE")
        invalid_acknowledgement = self.client.put(
            f"{config_url}/states/{stable_state['metric']}/acknowledgement",
            headers=self.headers,
            json={"note": "This should be rejected"},
        )
        self.assertEqual(
            invalid_acknowledgement.status_code,
            409,
            invalid_acknowledgement.text,
        )

        repeated = asyncio.run(
            evaluate_quality_alert_batch(
                self.app.state.session_factory,
                now=moment + timedelta(seconds=60),
                evaluation_interval_seconds=60,
            )
        )
        self.assertEqual((repeated.transitions, repeated.queued), (0, 0))

        silence_until = moment + timedelta(seconds=123)
        silenced = self.client.put(
            f"{config_url}/silence",
            headers=self.headers,
            json={
                "silenced_until": silence_until.isoformat(),
                "reason": "Release maintenance window",
            },
        )
        self.assertEqual(silenced.status_code, 200, silenced.text)
        self.assertEqual(silenced.json()["silence_reason"], "Release maintenance window")
        self.assertIsNotNone(silenced.json()["silenced_by"])
        self.assertEqual(silenced.json()["silenced_by_display_name"], "Integration Admin")

        asyncio.run(set_current_status("PASSED"))
        silence_suppressed = asyncio.run(
            evaluate_quality_alert_batch(
                self.app.state.session_factory,
                now=moment + timedelta(seconds=120),
                evaluation_interval_seconds=60,
            )
        )
        self.assertEqual(
            (
                silence_suppressed.transitions,
                silence_suppressed.queued,
                silence_suppressed.silence_suppressed,
            ),
            (2, 0, 2),
        )
        states_during_silence = self.client.get(
            f"{config_url}/states",
            headers=self.headers,
        )
        self.assertEqual(states_during_silence.status_code, 200, states_during_silence.text)
        previously_critical = [
            state
            for state in states_during_silence.json()
            if state["metric"] in {item["metric"] for item in critical_states}
        ]
        self.assertTrue(all(state["current_status"] == "STABLE" for state in previously_critical))
        self.assertTrue(
            all(state["active_notification_status"] == "CRITICAL" for state in previously_critical)
        )
        self.assertTrue(all(state["acknowledged_at"] is None for state in previously_critical))

        recovered = asyncio.run(
            evaluate_quality_alert_batch(
                self.app.state.session_factory,
                now=moment + timedelta(seconds=125),
                evaluation_interval_seconds=60,
            )
        )
        self.assertEqual((recovered.transitions, recovered.queued), (0, 2))
        cleared_silence = self.client.delete(
            f"{config_url}/silence",
            headers=self.headers,
        )
        self.assertEqual(cleared_silence.status_code, 200, cleared_silence.text)
        self.assertIsNone(cleared_silence.json()["silenced_until"])

        asyncio.run(set_current_status("FAILED"))
        suppressed = asyncio.run(
            evaluate_quality_alert_batch(
                self.app.state.session_factory,
                now=moment + timedelta(seconds=190),
                evaluation_interval_seconds=60,
            )
        )
        self.assertEqual(
            (suppressed.transitions, suppressed.queued, suppressed.cooldown_suppressed),
            (2, 0, 2),
        )
        retriggered = asyncio.run(
            evaluate_quality_alert_batch(
                self.app.state.session_factory,
                now=moment + timedelta(seconds=3700),
                evaluation_interval_seconds=60,
            )
        )
        self.assertEqual((retriggered.transitions, retriggered.queued), (0, 2))

        async def inspect_automatic_records() -> tuple[int, int, int, set[str], set[int]]:
            async with self.app.state.session_factory() as session:
                deliveries = tuple(
                    await session.scalars(
                        select(QualityWebhookDeliveryRecord).where(
                            QualityWebhookDeliveryRecord.project_id == UUID(project_id)
                        )
                    )
                )
                alert_states = tuple(
                    await session.scalars(
                        select(QualityAlertStateRecord).where(
                            QualityAlertStateRecord.project_id == UUID(project_id)
                        )
                    )
                )
                audit_count = await session.scalar(
                    select(func.count())
                    .select_from(AuditLogRecord)
                    .where(
                        AuditLogRecord.project_id == UUID(project_id),
                        AuditLogRecord.action == "project.quality_alert_queued",
                    )
                )
                operation_audit_count = await session.scalar(
                    select(func.count())
                    .select_from(AuditLogRecord)
                    .where(
                        AuditLogRecord.project_id == UUID(project_id),
                        AuditLogRecord.action.in_(
                            (
                                "project.quality_alert_acknowledged",
                                "project.quality_alert_acknowledgement_cleared",
                                "project.quality_alert_silenced",
                                "project.quality_alert_silence_cleared",
                            )
                        ),
                    )
                )
                return (
                    len(deliveries),
                    int(audit_count or 0),
                    int(operation_audit_count or 0),
                    {delivery.event_type for delivery in deliveries},
                    {
                        state.notification_sequence
                        for state in alert_states
                        if state.metric != "EXECUTION_RELIABILITY"
                    },
                )

        (
            delivery_count,
            audit_count,
            operation_audit_count,
            event_types,
            notification_sequences,
        ) = asyncio.run(inspect_automatic_records())
        self.assertEqual(delivery_count, 6)
        self.assertEqual(audit_count, 6)
        self.assertEqual(operation_audit_count, 5)
        self.assertEqual(
            event_types,
            {"quality.alert.triggered", "quality.alert.recovered"},
        )
        self.assertEqual(notification_sequences, {3})

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
