from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from testops.contracts import CaseResult, CaseResultStatus, RunResult, RunSnapshot, RunStatus
from testops.worker.container_execution import container_isolation_evidence
from testops.worker.isolated_executor import subprocess_isolation_evidence
from testops.worker.kubernetes_execution import kubernetes_isolation_evidence
from testops.worker.outbox import _worker_isolation_rank, _worker_supports_execution_isolation
from testops.worker.package_runtime import PackageRuntimeCatalog
from testops.worker.tasks import _reconcile_events, execute_run

ROOT = Path(__file__).resolve().parents[2]


class RecordingControlPlane:
    def __init__(self) -> None:
        self.events: list[tuple[UUID, dict[str, object]]] = []

    def report_event(self, run_id: UUID, event: dict[str, object]) -> None:
        self.events.append((run_id, event))


class WorkerObservabilityTests(unittest.TestCase):
    def test_dispatch_ranks_complete_kubernetes_isolation_above_container(self) -> None:
        container = SimpleNamespace(
            capabilities={
                "execution_isolation": {
                    "mode": "CONTAINER",
                    "dedicated_process": True,
                    "credential_scope": "RUN_SECRETS_ONLY",
                    "read_only_root_filesystem": True,
                    "network_policy": "DENY_ALL",
                    "resource_limits_enforced": True,
                    "memory_limit_bytes": 1024 * 1024 * 1024,
                    "cpu_limit_millis": 1000,
                    "pids_limit": 256,
                }
            }
        )
        kubernetes = SimpleNamespace(
            capabilities={
                "execution_isolation": {
                    "mode": "KUBERNETES",
                    "dedicated_process": True,
                    "credential_scope": "RUN_SECRETS_ONLY",
                    "read_only_root_filesystem": True,
                    "network_policy": "DENY_ALL",
                    "resource_limits_enforced": True,
                    "memory_limit_bytes": 1024 * 1024 * 1024,
                    "cpu_limit_millis": 1000,
                    "ephemeral_storage_limit_bytes": 2 * 1024 * 1024 * 1024,
                    "orchestrator_namespace": "testops-runs",
                    "service_account_name": "testops-runner",
                    "service_account_token_automounted": False,
                }
            }
        )
        self.assertTrue(_worker_supports_execution_isolation(container))
        self.assertTrue(_worker_supports_execution_isolation(kubernetes))
        self.assertGreater(_worker_isolation_rank(kubernetes), _worker_isolation_rank(container))

        kubernetes.capabilities["execution_isolation"]["service_account_token_automounted"] = True
        self.assertFalse(_worker_supports_execution_isolation(kubernetes))

    def test_terminal_run_is_ignored_before_local_result_recovery(self) -> None:
        job = RunSnapshot.model_validate_json(
            (ROOT / "tests/fixtures/run_snapshot.valid.json").read_text("utf-8")
        )
        control_plane = Mock()
        control_plane.run_state.return_value = {
            "status": "TIMED_OUT",
            "cancel_requested": True,
        }
        settings = SimpleNamespace(
            control_plane_url="http://control-plane.invalid",
            runner_callback_token="runner-token",
        )
        with (
            patch(
                "testops.worker.tasks.WorkerSettings.from_environment",
                return_value=settings,
            ),
            patch("testops.worker.tasks.ControlPlaneClient", return_value=control_plane),
            patch(
                "testops.worker.tasks._existing_result",
                side_effect=AssertionError("terminal Run must not read local result"),
            ),
        ):
            result = execute_run.run(job.model_dump(mode="json", exclude_none=True))

        self.assertEqual(result["status"], "TIMED_OUT")
        self.assertTrue(result["ignored"])

    def test_unavailable_package_fails_before_adapter_execution(self) -> None:
        job = RunSnapshot.model_validate_json(
            (ROOT / "tests/fixtures/run_snapshot.valid.json").read_text("utf-8")
        )
        control_plane = Mock()
        control_plane.run_state.return_value = {
            "status": "QUEUED",
            "cancel_requested": False,
        }
        with tempfile.TemporaryDirectory() as workspace_root:
            settings = SimpleNamespace(
                control_plane_url="http://control-plane.invalid",
                runner_callback_token="runner-token",
                runner_worker_key="worker-01",
                runner_version="0.27.0",
                workspace_root=workspace_root,
                runner_package_catalog=PackageRuntimeCatalog.from_json("[]"),
            )
            with (
                patch(
                    "testops.worker.tasks.WorkerSettings.from_environment",
                    return_value=settings,
                ),
                patch("testops.worker.tasks.ControlPlaneClient", return_value=control_plane),
                patch(
                    "testops.worker.tasks.PlaywrightWebAdapter",
                    side_effect=AssertionError("unavailable package must not reach an adapter"),
                ),
            ):
                response = execute_run.run(job.model_dump(mode="json", exclude_none=True))

        self.assertEqual(response["status"], "INFRA_ERROR")
        reported = control_plane.report_result.call_args.args[0]
        self.assertEqual(reported.status.value, "INFRA_ERROR")
        self.assertEqual(
            {case.failure_category for case in reported.case_results},
            {"AUTOMATION_PACKAGE_UNAVAILABLE"},
        )

    def test_subprocess_mode_executes_without_constructing_parent_adapter(self) -> None:
        job = RunSnapshot.model_validate_json(
            (ROOT / "tests/fixtures/run_snapshot.valid.json").read_text("utf-8")
        )
        moment = datetime.now(UTC)
        case = job.cases[0]
        isolated_result = RunResult(
            run_id=job.run_id,
            status=RunStatus.PASSED,
            started_at=moment,
            finished_at=moment,
            runner_version="0.27.0",
            case_results=(
                CaseResult(
                    case_id=case.case_id,
                    case_code=case.case_code,
                    status=CaseResultStatus.PASSED,
                    started_at=moment,
                    finished_at=moment,
                    duration_ms=0,
                ),
            ),
            execution_isolation=subprocess_isolation_evidence("0.27.0"),
        )
        package_catalog = PackageRuntimeCatalog.from_json(
            json.dumps(
                [
                    {
                        "runner_type": job.automation_package.runner_type,
                        "image_repository": job.automation_package.image_repository,
                        "digest": job.automation_package.digest,
                    }
                ]
            )
        )
        control_plane = Mock()
        control_plane.run_state.return_value = {
            "status": "QUEUED",
            "cancel_requested": False,
        }
        executor = Mock()
        executor.execute.return_value = isolated_result
        with tempfile.TemporaryDirectory() as workspace_root:
            settings = SimpleNamespace(
                control_plane_url="http://control-plane.invalid",
                runner_callback_token="runner-token",
                runner_worker_key="worker-01",
                runner_version="0.27.0",
                runner_execution_mode="SUBPROCESS",
                workspace_root=workspace_root,
                runner_package_catalog=package_catalog,
            )
            with (
                patch(
                    "testops.worker.tasks.WorkerSettings.from_environment",
                    return_value=settings,
                ),
                patch("testops.worker.tasks.ControlPlaneClient", return_value=control_plane),
                patch("testops.worker.tasks.SubprocessRunExecutor", return_value=executor),
                patch(
                    "testops.worker.tasks.PlaywrightWebAdapter",
                    side_effect=AssertionError("parent process must not construct an adapter"),
                ),
            ):
                response = execute_run.run(job.model_dump(mode="json", exclude_none=True))

        self.assertEqual(response["status"], "PASSED")
        executor.execute.assert_called_once_with(job)
        reported = control_plane.report_result.call_args.args[0]
        self.assertEqual(reported.execution_isolation.mode, "SUBPROCESS")

    def test_container_mode_uses_the_hard_isolation_executor(self) -> None:
        job = RunSnapshot.model_validate_json(
            (ROOT / "tests/fixtures/run_snapshot.valid.json").read_text("utf-8")
        )
        moment = datetime.now(UTC)
        case = job.cases[0]
        package_catalog = PackageRuntimeCatalog.from_json(
            json.dumps(
                [
                    {
                        "runner_type": job.automation_package.runner_type,
                        "image_repository": job.automation_package.image_repository,
                        "digest": job.automation_package.digest,
                    }
                ]
            )
        )
        control_plane = Mock()
        control_plane.run_state.return_value = {
            "status": "QUEUED",
            "cancel_requested": False,
        }
        with tempfile.TemporaryDirectory() as workspace_root:
            settings = SimpleNamespace(
                control_plane_url="http://control-plane.invalid",
                runner_callback_token="runner-token",
                runner_worker_key="worker-01",
                runner_version="0.28.0",
                runner_execution_mode="CONTAINER",
                workspace_root=workspace_root,
                runner_package_catalog=package_catalog,
                runner_container_network_policy="DENY_ALL",
                runner_container_memory_bytes=1024 * 1024 * 1024,
                runner_container_cpu_millis=1000,
                runner_container_pids_limit=256,
            )
            isolated_result = RunResult(
                run_id=job.run_id,
                status=RunStatus.PASSED,
                started_at=moment,
                finished_at=moment,
                runner_version="0.28.0",
                case_results=(
                    CaseResult(
                        case_id=case.case_id,
                        case_code=case.case_code,
                        status=CaseResultStatus.PASSED,
                        started_at=moment,
                        finished_at=moment,
                        duration_ms=0,
                    ),
                ),
                execution_isolation=container_isolation_evidence(settings, "sha256:" + "a" * 64),
            )
            executor = Mock()
            executor.execute.return_value = isolated_result
            with (
                patch(
                    "testops.worker.tasks.WorkerSettings.from_environment",
                    return_value=settings,
                ),
                patch("testops.worker.tasks.ControlPlaneClient", return_value=control_plane),
                patch("testops.worker.tasks.ContainerRunExecutor", return_value=executor),
                patch(
                    "testops.worker.tasks.PlaywrightWebAdapter",
                    side_effect=AssertionError("parent process must not construct an adapter"),
                ),
            ):
                response = execute_run.run(job.model_dump(mode="json", exclude_none=True))

        self.assertEqual(response["status"], "PASSED")
        executor.execute.assert_called_once_with(job)
        reported = control_plane.report_result.call_args.args[0]
        self.assertEqual(reported.execution_isolation.mode, "CONTAINER")

    def test_kubernetes_mode_uses_the_job_isolation_executor(self) -> None:
        job = RunSnapshot.model_validate_json(
            (ROOT / "tests/fixtures/run_snapshot.valid.json").read_text("utf-8")
        )
        moment = datetime.now(UTC)
        case = job.cases[0]
        package_catalog = PackageRuntimeCatalog.from_json(
            json.dumps(
                [
                    {
                        "runner_type": job.automation_package.runner_type,
                        "image_repository": job.automation_package.image_repository,
                        "digest": job.automation_package.digest,
                    }
                ]
            )
        )
        control_plane = Mock()
        control_plane.run_state.return_value = {
            "status": "QUEUED",
            "cancel_requested": False,
        }
        with tempfile.TemporaryDirectory() as workspace_root:
            settings = SimpleNamespace(
                control_plane_url="http://control-plane.invalid",
                runner_callback_token="runner-token",
                runner_worker_key="worker-01",
                runner_version="0.30.0",
                runner_execution_mode="KUBERNETES",
                workspace_root=workspace_root,
                runner_package_catalog=package_catalog,
                runner_kubernetes_namespace="testops-runs",
                runner_kubernetes_service_account="testops-runner",
                runner_kubernetes_network_policy="DENY_ALL",
                runner_kubernetes_memory_bytes=1024 * 1024 * 1024,
                runner_kubernetes_cpu_millis=1000,
                runner_kubernetes_ephemeral_storage_bytes=2 * 1024 * 1024 * 1024,
            )
            isolated_result = RunResult(
                run_id=job.run_id,
                status=RunStatus.PASSED,
                started_at=moment,
                finished_at=moment,
                runner_version="0.30.0",
                case_results=(
                    CaseResult(
                        case_id=case.case_id,
                        case_code=case.case_code,
                        status=CaseResultStatus.PASSED,
                        started_at=moment,
                        finished_at=moment,
                        duration_ms=0,
                    ),
                ),
                execution_isolation=kubernetes_isolation_evidence(
                    settings, job.automation_package.digest
                ),
            )
            executor = Mock()
            executor.execute.return_value = isolated_result
            with (
                patch(
                    "testops.worker.tasks.WorkerSettings.from_environment",
                    return_value=settings,
                ),
                patch("testops.worker.tasks.ControlPlaneClient", return_value=control_plane),
                patch("testops.worker.tasks.KubernetesRunExecutor", return_value=executor),
                patch(
                    "testops.worker.tasks.PlaywrightWebAdapter",
                    side_effect=AssertionError("parent process must not construct an adapter"),
                ),
            ):
                response = execute_run.run(job.model_dump(mode="json", exclude_none=True))

        self.assertEqual(response["status"], "PASSED")
        executor.execute.assert_called_once_with(job)
        reported = control_plane.report_result.call_args.args[0]
        self.assertEqual(reported.execution_isolation.mode, "KUBERNETES")

    def test_local_events_are_replayed_in_file_order(self) -> None:
        run_id = uuid4()
        documents = [
            {"event": "run_started", "run_id": str(run_id), "at": "2026-08-12T08:00:00Z"},
            {
                "event": "case_started",
                "run_id": str(run_id),
                "case_code": "TC-LOGIN-001",
                "at": "2026-08-12T08:00:01Z",
            },
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = Path(temporary_directory) / str(run_id)
            run_directory.mkdir()
            (run_directory / "events.jsonl").write_text(
                "".join(json.dumps(document) + "\n" for document in documents),
                encoding="utf-8",
            )
            client = RecordingControlPlane()

            _reconcile_events(temporary_directory, run_id, client)  # type: ignore[arg-type]

        self.assertEqual([event for _run_id, event in client.events], documents)
        self.assertTrue(all(recorded_run_id == run_id for recorded_run_id, _ in client.events))

    def test_invalid_local_event_stops_result_delivery(self) -> None:
        run_id = uuid4()
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = Path(temporary_directory) / str(run_id)
            run_directory.mkdir()
            (run_directory / "events.jsonl").write_text("not-json\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "line 1"):
                _reconcile_events(  # type: ignore[arg-type]
                    temporary_directory,
                    run_id,
                    RecordingControlPlane(),
                )


if __name__ == "__main__":
    unittest.main()
