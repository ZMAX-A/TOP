from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from testops.contracts import AutomationPackageRef
from testops.worker.config import WorkerSettings
from testops.worker.package_runtime import (
    PackageRuntimeCatalog,
    PackageRuntimeUnavailable,
)

DIGEST = "sha256:" + "a" * 64


def package_ref(*, digest: str = DIGEST) -> AutomationPackageRef:
    return AutomationPackageRef(
        name="yanjia-web",
        version="1.0.0",
        runner_type="WEB_PLAYWRIGHT",
        image_repository="registry.example.invalid/testops/yanjia-web",
        digest=digest,
    )


class PackageRuntimeCatalogTests(unittest.TestCase):
    def test_exact_immutable_runtime_is_resolved_and_advertised(self) -> None:
        runtime = package_ref()
        catalog = PackageRuntimeCatalog.from_json(
            json.dumps(
                [
                    {
                        "runner_type": runtime.runner_type,
                        "image_repository": runtime.image_repository,
                        "digest": runtime.digest,
                    }
                ]
            )
        )

        self.assertEqual(catalog.require(runtime).digest, DIGEST)
        self.assertEqual(
            catalog.capability_payload(),
            [
                {
                    "runner_type": "WEB_PLAYWRIGHT",
                    "image_repository": "registry.example.invalid/testops/yanjia-web",
                    "digest": DIGEST,
                }
            ],
        )

    def test_digest_mismatch_fails_closed(self) -> None:
        catalog = PackageRuntimeCatalog.from_json(
            json.dumps(
                [
                    {
                        "runner_type": "WEB_PLAYWRIGHT",
                        "image_repository": "registry.example.invalid/testops/yanjia-web",
                        "digest": DIGEST,
                    }
                ]
            )
        )

        with self.assertRaisesRegex(PackageRuntimeUnavailable, "does not host"):
            catalog.require(package_ref(digest="sha256:" + "b" * 64))

    def test_mutable_or_duplicate_catalog_entries_are_rejected(self) -> None:
        mutable = json.dumps(
            [
                {
                    "runner_type": "WEB_PLAYWRIGHT",
                    "image_repository": "yanjia-web:latest",
                    "digest": DIGEST,
                }
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "invalid runtime reference"):
            PackageRuntimeCatalog.from_json(mutable)

        duplicated = json.dumps(
            [
                {
                    "runner_type": "WEB_PLAYWRIGHT",
                    "image_repository": "testops-worker",
                    "digest": DIGEST,
                },
                {
                    "runner_type": "WEB_PLAYWRIGHT",
                    "image_repository": "testops-worker",
                    "digest": DIGEST,
                },
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            PackageRuntimeCatalog.from_json(duplicated)

    def test_catalog_shape_and_size_are_bounded(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "JSON array"):
            PackageRuntimeCatalog.from_json("{}")
        oversized = [
            {
                "runner_type": "WEB_PLAYWRIGHT",
                "image_repository": f"registry.example.invalid/testops/package-{index}",
                "digest": DIGEST,
            }
            for index in range(51)
        ]
        with self.assertRaisesRegex(RuntimeError, "more than 50"):
            PackageRuntimeCatalog.from_json(json.dumps(oversized))

    def test_registered_worker_advertises_catalog_and_requires_it(self) -> None:
        runtime_payload = {
            "runner_type": "WEB_PLAYWRIGHT",
            "image_repository": "testops-worker",
            "digest": DIGEST,
        }
        environment = {
            "RUNNER_CALLBACK_TOKEN": "runner-token",
            "RUNNER_WORKER_KEY": "worker-01",
            "RUNNER_POOL_KEY": "default-web",
            "RUNNER_PACKAGE_CATALOG": json.dumps([runtime_payload]),
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = WorkerSettings.from_environment()
        self.assertEqual(
            settings.runner_capabilities["automation_packages"],
            [runtime_payload],
        )
        self.assertEqual(settings.runner_package_catalog.capability_payload(), [runtime_payload])
        self.assertEqual(settings.runner_execution_mode, "SUBPROCESS")
        self.assertEqual(
            settings.runner_capabilities["execution_isolation"],
            {
                "mode": "SUBPROCESS",
                "dedicated_process": True,
                "credential_scope": "RUN_SECRETS_ONLY",
                "read_only_root_filesystem": False,
                "network_policy": "WORKER_DEFAULT",
                "resource_limits_enforced": False,
            },
        )

        environment.pop("RUNNER_PACKAGE_CATALOG")
        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaisesRegex(RuntimeError, "required for a registered Runner Worker"),
        ):
            WorkerSettings.from_environment()

    def test_execution_isolation_capability_cannot_be_overridden(self) -> None:
        environment = {
            "RUNNER_CALLBACK_TOKEN": "runner-token",
            "RUNNER_CAPABILITIES": json.dumps(
                {
                    "target_types": ["WEB"],
                    "browsers": ["chromium"],
                    "labels": {},
                    "execution_isolation": {"mode": "CONTAINER"},
                }
            ),
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaisesRegex(RuntimeError, "cannot be overridden"),
        ):
            WorkerSettings.from_environment()

    def test_execution_isolation_settings_are_bounded(self) -> None:
        base_environment = {"RUNNER_CALLBACK_TOKEN": "runner-token"}
        with (
            patch.dict(
                os.environ,
                {**base_environment, "RUNNER_EXECUTION_MODE": "PROCESS"},
                clear=True,
            ),
            self.assertRaisesRegex(RuntimeError, "IN_PROCESS, SUBPROCESS, CONTAINER or KUBERNETES"),
        ):
            WorkerSettings.from_environment()
        with (
            patch.dict(
                os.environ,
                {**base_environment, "RUNNER_EXECUTOR_TIMEOUT_SECONDS": "59"},
                clear=True,
            ),
            self.assertRaisesRegex(RuntimeError, "between 60 and 86400"),
        ):
            WorkerSettings.from_environment()

    def test_container_execution_advertises_exact_hard_limits(self) -> None:
        environment = {
            "RUNNER_CALLBACK_TOKEN": "runner-token",
            "RUNNER_EXECUTION_MODE": "CONTAINER",
            "RUNNER_CONTAINER_IMAGE": "testops-worker:0.28.0",
            "RUNNER_CONTAINER_NETWORK_POLICY": "ALLOWLIST",
            "RUNNER_CONTAINER_NETWORK": "testops-runner-sandbox",
            "RUNNER_CONTAINER_MEMORY_MIB": "768",
            "RUNNER_CONTAINER_CPU_MILLIS": "1250",
            "RUNNER_CONTAINER_PIDS_LIMIT": "192",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = WorkerSettings.from_environment()

        self.assertEqual(settings.runner_execution_mode, "CONTAINER")
        self.assertEqual(settings.runner_container_memory_bytes, 768 * 1024 * 1024)
        self.assertEqual(
            settings.runner_capabilities["execution_isolation"],
            {
                "mode": "CONTAINER",
                "dedicated_process": True,
                "credential_scope": "RUN_SECRETS_ONLY",
                "read_only_root_filesystem": True,
                "network_policy": "ALLOWLIST",
                "resource_limits_enforced": True,
                "memory_limit_bytes": 768 * 1024 * 1024,
                "cpu_limit_millis": 1250,
                "pids_limit": 192,
            },
        )

        environment.pop("RUNNER_CONTAINER_NETWORK")
        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaisesRegex(RuntimeError, "required for ALLOWLIST"),
        ):
            WorkerSettings.from_environment()


if __name__ == "__main__":
    unittest.main()
