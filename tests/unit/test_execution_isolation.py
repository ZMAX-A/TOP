from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from testops.contracts import CaseResult, CaseResultStatus, RunResult, RunSnapshot, RunStatus
from testops.worker.execution_isolation import forward_new_events, isolated_child_environment
from testops.worker.isolated_executor import (
    execute_isolated_snapshot,
    subprocess_isolation_evidence,
)

ROOT = Path(__file__).resolve().parents[2]


class ExecutionIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.job = RunSnapshot.model_validate_json(
            (ROOT / "tests/fixtures/run_snapshot.valid.json").read_text("utf-8")
        )

    def test_child_environment_contains_only_bound_run_secrets(self) -> None:
        parent_environment = {
            "PATH": os.defpath,
            "PYTHONPATH": os.pathsep.join(["packages/contracts/src", "services/worker/src"]),
            "DATABASE_URL": "postgresql://control-plane-secret",
            "REDIS_URL": "redis://broker-secret",
            "CONTROL_PLANE_URL": "https://control-plane.invalid",
            "RUNNER_CALLBACK_TOKEN": "callback-secret",
            "MINIO_SECRET_KEY": "minio-secret",
            "TESTOPS_SECRET_TEST_USERNAME": "bound-user",
            "TESTOPS_SECRET_TEST_PASSWORD": "bound-password",
            "TESTOPS_SECRET_UNRELATED": "unrelated-secret",
        }
        with tempfile.TemporaryDirectory() as sandbox_home:
            environment = isolated_child_environment(
                self.job,
                sandbox_home=sandbox_home,
                executor_version="0.27.0",
                environ=parent_environment,
            )

        self.assertEqual(environment["TESTOPS_SECRET_TEST_USERNAME"], "bound-user")
        self.assertEqual(environment["TESTOPS_SECRET_TEST_PASSWORD"], "bound-password")
        self.assertNotIn("TESTOPS_SECRET_UNRELATED", environment)
        for excluded_name in (
            "DATABASE_URL",
            "REDIS_URL",
            "CONTROL_PLANE_URL",
            "RUNNER_CALLBACK_TOKEN",
            "MINIO_SECRET_KEY",
        ):
            self.assertNotIn(excluded_name, environment)
        self.assertEqual(environment["HOME"], str(Path(sandbox_home).resolve()))
        self.assertEqual(environment["TEMP"], str(Path(sandbox_home).resolve()))
        self.assertEqual(environment["TESTOPS_EXECUTOR_MODE"], "SUBPROCESS")
        self.assertEqual(environment["TESTOPS_EXECUTOR_VERSION"], "0.27.0")
        self.assertTrue(
            all(Path(entry).is_absolute() for entry in environment["PYTHONPATH"].split(os.pathsep))
        )

    def test_subprocess_evidence_does_not_claim_unimplemented_hard_isolation(self) -> None:
        evidence = subprocess_isolation_evidence("0.27.0")

        self.assertEqual(evidence.mode, "SUBPROCESS")
        self.assertTrue(evidence.dedicated_process)
        self.assertEqual(evidence.credential_scope, "RUN_SECRETS_ONLY")
        self.assertFalse(evidence.read_only_root_filesystem)
        self.assertEqual(evidence.network_policy, "WORKER_DEFAULT")
        self.assertFalse(evidence.resource_limits_enforced)

    def test_parent_forwards_only_complete_new_child_events(self) -> None:
        control_plane = Mock()
        with tempfile.TemporaryDirectory() as workspace_root:
            run_root = Path(workspace_root) / str(self.job.run_id)
            run_root.mkdir()
            events_path = run_root / "events.jsonl"
            first = json.dumps({"event": "run_started", "run_id": str(self.job.run_id)})
            second = json.dumps({"event": "case_started", "case_code": "TC-LOGIN-001"})
            events_path.write_text(first + "\n" + second[:8], encoding="utf-8")

            offset = forward_new_events(workspace_root, self.job, control_plane, 0)
            with events_path.open("a", encoding="utf-8") as stream:
                stream.write(second[8:] + "\n")
            final_offset = forward_new_events(
                workspace_root,
                self.job,
                control_plane,
                offset,
            )

        self.assertGreater(final_offset, offset)
        self.assertEqual(
            [call.args[1]["event"] for call in control_plane.report_event.call_args_list],
            ["run_started", "case_started"],
        )

    def test_child_enriches_and_persists_adapter_result(self) -> None:
        moment = datetime.now(UTC)
        case = self.job.cases[0]
        result = RunResult(
            run_id=self.job.run_id,
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
        )
        adapter = Mock()
        adapter.execute.return_value = result
        with tempfile.TemporaryDirectory() as workspace_root:
            run_root = Path(workspace_root) / str(self.job.run_id)
            run_root.mkdir()
            (run_root / "run-result.json").write_text(
                json.dumps(result.model_dump(mode="json")),
                encoding="utf-8",
            )
            with patch(
                "testops.worker.isolated_executor.PlaywrightWebAdapter",
                return_value=adapter,
            ):
                enriched = execute_isolated_snapshot(
                    self.job.model_dump(mode="json", exclude_none=True),
                    workspace_root=workspace_root,
                    environ={
                        "TESTOPS_SECRET_TEST_USERNAME": "user",
                        "TESTOPS_SECRET_TEST_PASSWORD": "password",
                    },
                    executor_version="0.27.0",
                )

            persisted = RunResult.model_validate_json(
                (run_root / "run-result.json").read_text("utf-8")
            )

        adapter.prepare.assert_called_once_with(self.job)
        self.assertEqual(enriched.execution_isolation, persisted.execution_isolation)
        self.assertEqual(persisted.execution_isolation.mode, "SUBPROCESS")


if __name__ == "__main__":
    unittest.main()
