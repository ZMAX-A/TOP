from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from testops.contracts import CaseResult, CaseResultStatus, RunResult, RunSnapshot, RunStatus
from testops.worker.container_execution import (
    ContainerRunExecutor,
    container_create_options,
    container_environment,
    container_isolation_evidence,
    extract_run_archive,
)
from testops.worker.execution_isolation import IsolatedExecutionError
from testops.worker.isolated_executor import isolation_evidence_from_environment

ROOT = Path(__file__).resolve().parents[2]


class NotFoundError(RuntimeError):
    status_code = 404


def archive_bytes(files: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    destination = io.BytesIO()
    with tarfile.open(fileobj=destination, mode="w") as archive:
        for name, payload in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(payload))
        if symlink:
            member = tarfile.TarInfo(symlink)
            member.type = tarfile.SYMTYPE
            member.linkname = "outside"
            archive.addfile(member)
    return destination.getvalue()


class ContainerExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.job = RunSnapshot.model_validate_json(
            (ROOT / "tests/fixtures/run_snapshot.valid.json").read_text("utf-8")
        )
        self.settings = SimpleNamespace(
            workspace_root="unused",
            runner_version="0.28.0",
            runner_executor_poll_seconds=0.1,
            runner_executor_timeout_seconds=60,
            runner_container_image="testops-worker:0.28.0",
            runner_container_network_policy="ALLOWLIST",
            runner_container_network="testops-runner-sandbox",
            runner_container_memory_bytes=768 * 1024 * 1024,
            runner_container_cpu_millis=1250,
            runner_container_pids_limit=192,
            runner_container_shm_bytes=512 * 1024 * 1024,
        )
        self.image_id = "sha256:" + "a" * 64

    def test_environment_contains_only_bound_run_secrets_and_exact_evidence(self) -> None:
        environment = container_environment(
            self.job,
            self.settings,
            self.image_id,
            environ={
                "DATABASE_URL": "postgresql://secret",
                "RUNNER_CALLBACK_TOKEN": "callback-secret",
                "TESTOPS_SECRET_TEST_USERNAME": "user",
                "TESTOPS_SECRET_TEST_PASSWORD": "password",
                "TESTOPS_SECRET_UNRELATED": "unrelated",
            },
        )

        self.assertEqual(environment["TESTOPS_SECRET_TEST_USERNAME"], "user")
        self.assertEqual(environment["TESTOPS_SECRET_TEST_PASSWORD"], "password")
        self.assertNotIn("TESTOPS_SECRET_UNRELATED", environment)
        self.assertNotIn("DATABASE_URL", environment)
        self.assertNotIn("RUNNER_CALLBACK_TOKEN", environment)
        evidence = isolation_evidence_from_environment(environment)
        self.assertEqual(evidence, container_isolation_evidence(self.settings, self.image_id))

    def test_container_options_enforce_hard_isolation(self) -> None:
        options = container_create_options(
            self.job,
            self.settings,
            self.image_id,
            "testops-run-volume",
            "testops-run-input",
            environ={},
        )

        self.assertTrue(options["read_only"])
        self.assertEqual(options["network"], "testops-runner-sandbox")
        self.assertEqual(options["cap_drop"], ["ALL"])
        self.assertEqual(options["security_opt"], ["no-new-privileges:true"])
        self.assertEqual(options["mem_limit"], 768 * 1024 * 1024)
        self.assertEqual(options["nano_cpus"], 1_250_000_000)
        self.assertEqual(options["pids_limit"], 192)
        self.assertEqual(options["user"], "pwuser")
        self.assertNotIn("/var/run/docker.sock", json.dumps(options))

        deny_all = SimpleNamespace(**vars(self.settings))
        deny_all.runner_container_network_policy = "DENY_ALL"
        deny_all.runner_container_network = None
        denied = container_create_options(
            self.job,
            deny_all,
            self.image_id,
            "testops-run-volume",
            "testops-run-input",
            environ={},
        )
        self.assertEqual(denied["network"], "none")

    def test_archive_import_rejects_traversal_and_special_files(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            valid = archive_bytes(
                {
                    f"{self.job.run_id}/events.jsonl": b'{"event":"run_started"}\n',
                    f"{self.job.run_id}/artifact.txt": b"artifact",
                }
            )
            extract_run_archive([valid], workspace_root=workspace, run_id=self.job.run_id)
            imported = Path(workspace) / str(self.job.run_id)
            self.assertEqual((imported / "artifact.txt").read_bytes(), b"artifact")

        with tempfile.TemporaryDirectory() as workspace:
            traversal = archive_bytes({"../escape.txt": b"escape"})
            with self.assertRaisesRegex(IsolatedExecutionError, "escaped"):
                extract_run_archive([traversal], workspace_root=workspace, run_id=self.job.run_id)
            self.assertFalse((Path(workspace).parent / "escape.txt").exists())

        with tempfile.TemporaryDirectory() as workspace:
            special = archive_bytes({}, symlink=f"{self.job.run_id}/link")
            with self.assertRaisesRegex(IsolatedExecutionError, "special file"):
                extract_run_archive([special], workspace_root=workspace, run_id=self.job.run_id)

    def test_executor_validates_image_label_imports_result_and_cleans_resources(self) -> None:
        evidence = container_isolation_evidence(self.settings, self.image_id)
        moment = datetime.now(UTC)
        case = self.job.cases[0]
        result = RunResult(
            run_id=self.job.run_id,
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
            execution_isolation=evidence,
        )
        run_archive = archive_bytes(
            {
                f"{self.job.run_id}/run-result.json": result.model_dump_json().encode("utf-8"),
            }
        )
        image = SimpleNamespace(
            id=self.image_id,
            attrs={
                "Config": {
                    "Labels": {
                        "io.testops.package.digest": self.job.automation_package.digest,
                        "io.testops.runtime.uid": "1001",
                        "io.testops.runtime.gid": "1001",
                    }
                }
            },
        )
        volume = Mock()
        volume.name = f"testops-run-{self.job.run_id.hex}"
        input_volume = Mock()
        input_volume.name = f"testops-run-{self.job.run_id.hex}-input"
        staging_container = Mock()
        staging_container.put_archive.return_value = True
        container = Mock()
        container.attrs = {
            "Image": self.image_id,
            "Config": {"User": "pwuser"},
            "HostConfig": {
                "ReadonlyRootfs": True,
                "Privileged": False,
                "NetworkMode": "testops-runner-sandbox",
                "Memory": 768 * 1024 * 1024,
                "NanoCpus": 1_250_000_000,
                "PidsLimit": 192,
                "ShmSize": 512 * 1024 * 1024,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "Tmpfs": {"/tmp": "options", "/home/pwuser": "options"},
            },
            "Mounts": [
                {
                    "Type": "volume",
                    "Destination": "/var/lib/testops/runs",
                    "RW": True,
                },
                {"Type": "volume", "Destination": "/run/testops-input", "RW": False},
            ],
            "State": {"Running": False, "ExitCode": 0},
        }
        container.put_archive.return_value = True

        def get_archive(path: str):
            if path.endswith("events.jsonl"):
                raise NotFoundError("not found")
            return iter([run_archive]), {}

        container.get_archive.side_effect = get_archive
        client = SimpleNamespace(
            images=SimpleNamespace(get=Mock(return_value=image)),
            volumes=SimpleNamespace(
                get=Mock(side_effect=NotFoundError("not found")),
                create=Mock(side_effect=[volume, input_volume]),
            ),
            containers=SimpleNamespace(
                get=Mock(side_effect=NotFoundError("not found")),
                create=Mock(side_effect=[staging_container, container]),
            ),
        )
        control_plane = Mock()
        with tempfile.TemporaryDirectory() as workspace:
            self.settings.workspace_root = workspace
            actual = ContainerRunExecutor(
                self.settings,
                control_plane,
                environ={},
                client=client,
            ).execute(self.job)

        self.assertEqual(actual.execution_isolation, evidence)
        create_options = client.containers.create.call_args_list[-1].kwargs
        self.assertEqual(create_options["image"], self.image_id)
        staging_container.put_archive.assert_called_once()
        staging_container.remove.assert_called_once_with(force=True)
        container.start.assert_called_once_with()
        container.remove.assert_called_once_with(force=True)
        volume.remove.assert_called_once_with(force=True)
        input_volume.remove.assert_called_once_with(force=True)

    def test_executor_rejects_an_image_with_the_wrong_package_digest(self) -> None:
        image = SimpleNamespace(
            id=self.image_id,
            attrs={
                "Config": {
                    "Labels": {
                        "io.testops.package.digest": "sha256:" + "f" * 64,
                        "io.testops.runtime.uid": "1001",
                        "io.testops.runtime.gid": "1001",
                    }
                }
            },
        )
        client = SimpleNamespace(images=SimpleNamespace(get=Mock(return_value=image)))
        with self.assertRaisesRegex(IsolatedExecutionError, "does not match"):
            ContainerRunExecutor(self.settings, Mock(), environ={}, client=client).execute(self.job)


if __name__ == "__main__":
    unittest.main()
