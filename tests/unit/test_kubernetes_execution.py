from __future__ import annotations

import base64
import copy
import io
import json
import os
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from testops.contracts import CaseResult, CaseResultStatus, RunResult, RunSnapshot, RunStatus
from testops.worker.config import WorkerSettings
from testops.worker.execution_isolation import IsolatedExecutionError
from testops.worker.isolated_executor import isolation_evidence_from_environment
from testops.worker.kubernetes_execution import (
    FINISHED_COMMAND,
    KubernetesClientBundle,
    KubernetesRunExecutor,
    kubernetes_isolation_evidence,
    kubernetes_resources,
    verify_kubernetes_isolation,
)

ROOT = Path(__file__).resolve().parents[2]


class NotFoundError(RuntimeError):
    status = 404


def archive_bytes(files: dict[str, bytes]) -> bytes:
    destination = io.BytesIO()
    with tarfile.open(fileobj=destination, mode="w") as archive:
        for name, payload in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(payload))
    return destination.getvalue()


class KubernetesExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.job = RunSnapshot.model_validate_json(
            (ROOT / "tests/fixtures/run_snapshot.valid.json").read_text("utf-8")
        )
        self.settings = SimpleNamespace(
            workspace_root="unused",
            runner_version="0.30.0",
            runner_executor_poll_seconds=0.1,
            runner_executor_timeout_seconds=60,
            runner_kubernetes_namespace="testops-runs",
            runner_kubernetes_service_account="testops-runner",
            runner_kubernetes_network_policy="ALLOWLIST",
            runner_kubernetes_network_policy_enforced=True,
            runner_kubernetes_allow_cidrs=("10.20.0.0/16", "2001:db8::/64"),
            runner_kubernetes_memory_bytes=768 * 1024 * 1024,
            runner_kubernetes_cpu_millis=1250,
            runner_kubernetes_ephemeral_storage_bytes=2 * 1024 * 1024 * 1024,
            runner_kubernetes_cleanup_ttl_seconds=300,
            runner_kubernetes_in_cluster=True,
            runner_kubernetes_context=None,
        )

    def test_manifest_uses_restricted_pod_and_only_bound_run_secrets(self) -> None:
        documents = kubernetes_resources(
            self.job,
            self.settings,
            environ={
                "DATABASE_URL": "postgresql://secret",
                "RUNNER_CALLBACK_TOKEN": "callback-secret",
                "TESTOPS_SECRET_TEST_USERNAME": "user",
                "TESTOPS_SECRET_TEST_PASSWORD": "password",
                "TESTOPS_SECRET_UNRELATED": "unrelated",
            },
        )

        secret = documents["secret"]
        self.assertIsNotNone(secret)
        self.assertEqual(
            secret["stringData"],
            {
                "TESTOPS_SECRET_TEST_USERNAME": "user",
                "TESTOPS_SECRET_TEST_PASSWORD": "password",
            },
        )
        serialized = json.dumps(documents)
        self.assertNotIn("postgresql://secret", serialized)
        self.assertNotIn("callback-secret", serialized)
        self.assertNotIn("TESTOPS_SECRET_UNRELATED", serialized)
        pod_spec = documents["job"]["spec"]["template"]["spec"]
        self.assertFalse(pod_spec["automountServiceAccountToken"])
        self.assertEqual(pod_spec["serviceAccountName"], "testops-runner")
        self.assertFalse(pod_spec["hostNetwork"])
        self.assertFalse(pod_spec["hostPID"])
        self.assertFalse(pod_spec["hostIPC"])
        runner = pod_spec["containers"][0]
        self.assertEqual(
            runner["image"],
            f"{self.job.automation_package.image_repository}@{self.job.automation_package.digest}",
        )
        self.assertTrue(runner["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(runner["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(runner["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertEqual(runner["resources"]["limits"], runner["resources"]["requests"])
        self.assertNotIn("hostPath", serialized)
        self.assertNotIn("docker.sock", serialized)
        policy = documents["network_policy"]["spec"]
        self.assertEqual(policy["policyTypes"], ["Egress"])
        self.assertEqual(
            [entry["ipBlock"]["cidr"] for entry in policy["egress"][0]["to"]],
            ["10.20.0.0/16", "2001:db8::/64"],
        )

        child_environment = {
            entry["name"]: entry["value"] for entry in runner["env"] if "value" in entry
        }
        self.assertEqual(
            isolation_evidence_from_environment(child_environment),
            kubernetes_isolation_evidence(self.settings, self.job.automation_package.digest),
        )

    def test_runtime_verification_rejects_mutated_pod_security(self) -> None:
        documents = kubernetes_resources(self.job, self.settings, environ={})
        pod = {
            "metadata": {"name": "run-pod"},
            "spec": copy.deepcopy(documents["job"]["spec"]["template"]["spec"]),
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {"imageID": "docker-pullable://runtime@" + self.job.automation_package.digest}
                ],
            },
        }
        image = (
            f"{self.job.automation_package.image_repository}@{self.job.automation_package.digest}"
        )
        self.assertEqual(
            verify_kubernetes_isolation(
                documents["job"],
                pod,
                documents["network_policy"],
                self.settings,
                image,
            ),
            self.job.automation_package.digest,
        )

        pod["spec"]["hostNetwork"] = True
        with self.assertRaisesRegex(IsolatedExecutionError, "hostNetwork"):
            verify_kubernetes_isolation(
                documents["job"],
                pod,
                documents["network_policy"],
                self.settings,
                image,
            )

    def test_executor_exports_result_and_cleans_every_run_resource(self) -> None:
        documents = kubernetes_resources(
            self.job,
            self.settings,
            environ={
                "TESTOPS_SECRET_TEST_USERNAME": "user",
                "TESTOPS_SECRET_TEST_PASSWORD": "password",
            },
        )
        evidence = kubernetes_isolation_evidence(self.settings, self.job.automation_package.digest)
        moment = datetime.now(UTC)
        case = self.job.cases[0]
        result = RunResult(
            run_id=self.job.run_id,
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
            execution_isolation=evidence,
        )
        archive = archive_bytes(
            {f"{self.job.run_id}/run-result.json": result.model_dump_json().encode("utf-8")}
        )
        pod = {
            "metadata": {"name": "testops-run-pod"},
            "spec": copy.deepcopy(documents["job"]["spec"]["template"]["spec"]),
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {"imageID": "containerd://runtime@" + self.job.automation_package.digest}
                ],
            },
        }
        batch = Mock()
        batch.read_namespaced_job.side_effect = [
            NotFoundError("not found"),
            documents["job"],
        ]
        core = Mock()
        core.read_namespaced_secret.side_effect = NotFoundError("not found")
        core.read_namespaced_config_map.side_effect = NotFoundError("not found")
        core.list_namespaced_pod.return_value = {"items": [pod]}
        networking = Mock()
        networking.read_namespaced_network_policy.side_effect = [
            NotFoundError("not found"),
            documents["network_policy"],
        ]

        def stream(_method, _pod_name, _namespace, *, command, **_kwargs):
            if command[0] == "base64":
                return base64.b64encode(archive).decode("ascii")
            if command[0] == "/bin/sh" and command[2] == FINISHED_COMMAND:
                return "0"
            if command[0] == "/bin/sh" and "events.jsonl" in command[2]:
                return "0"
            if command[0] == "/bin/sh" and "tar -C" in command[2]:
                return str(len(archive))
            raise AssertionError(f"unexpected Pod exec command: {command}")

        clients = KubernetesClientBundle(
            batch=batch,
            core=core,
            networking=networking,
            stream=stream,
        )
        control_plane = Mock()
        control_plane.cancel_requested.return_value = False
        with tempfile.TemporaryDirectory() as workspace:
            self.settings.workspace_root = workspace
            actual = KubernetesRunExecutor(
                self.settings,
                control_plane,
                environ={
                    "TESTOPS_SECRET_TEST_USERNAME": "user",
                    "TESTOPS_SECRET_TEST_PASSWORD": "password",
                },
                clients=clients,
            ).execute(self.job)

        self.assertEqual(actual.execution_isolation, evidence)
        batch.create_namespaced_job.assert_called_once()
        core.create_namespaced_config_map.assert_called_once()
        core.create_namespaced_secret.assert_called_once()
        networking.create_namespaced_network_policy.assert_called_once()
        batch.delete_namespaced_job.assert_called_once()
        core.delete_namespaced_config_map.assert_called_once()
        core.delete_namespaced_secret.assert_called_once()
        networking.delete_namespaced_network_policy.assert_called_once()

    def test_kubernetes_settings_require_operator_network_policy_attestation(self) -> None:
        environment = {
            "RUNNER_CALLBACK_TOKEN": "runner-token",
            "RUNNER_EXECUTION_MODE": "KUBERNETES",
            "RUNNER_KUBERNETES_NETWORK_POLICY": "ALLOWLIST",
            "RUNNER_KUBERNETES_NETWORK_POLICY_ENFORCED": "true",
            "RUNNER_KUBERNETES_ALLOW_CIDRS": "10.20.0.1/16,2001:db8::/64",
            "RUNNER_KUBERNETES_MEMORY_MIB": "768",
            "RUNNER_KUBERNETES_CPU_MILLIS": "1250",
            "RUNNER_KUBERNETES_EPHEMERAL_STORAGE_MIB": "2048",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = WorkerSettings.from_environment()
        self.assertEqual(settings.runner_execution_mode, "KUBERNETES")
        self.assertEqual(settings.runner_kubernetes_allow_cidrs, ("10.20.0.0/16", "2001:db8::/64"))
        self.assertEqual(
            settings.runner_capabilities["execution_isolation"],
            {
                "mode": "KUBERNETES",
                "dedicated_process": True,
                "credential_scope": "RUN_SECRETS_ONLY",
                "read_only_root_filesystem": True,
                "network_policy": "ALLOWLIST",
                "resource_limits_enforced": True,
                "memory_limit_bytes": 768 * 1024 * 1024,
                "cpu_limit_millis": 1250,
                "ephemeral_storage_limit_bytes": 2048 * 1024 * 1024,
                "orchestrator_namespace": "testops-runs",
                "service_account_name": "testops-runner",
                "service_account_token_automounted": False,
            },
        )

        environment["RUNNER_KUBERNETES_NETWORK_POLICY_ENFORCED"] = "false"
        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaisesRegex(RuntimeError, "NETWORK_POLICY_ENFORCED"),
        ):
            WorkerSettings.from_environment()


if __name__ == "__main__":
    unittest.main()
