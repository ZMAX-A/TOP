from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from scripts.smoke_compose import PACKAGE_VERSION

from testops.api.main import API_VERSION

ROOT = Path(__file__).resolve().parents[2]


def test_release_version_is_coherent_across_runtime_and_packaging() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    frontend = json.loads((ROOT / "apps/frontend/package.json").read_text(encoding="utf-8"))
    frontend_lock = json.loads(
        (ROOT / "apps/frontend/package-lock.json").read_text(encoding="utf-8")
    )
    environment = (ROOT / ".env.example").read_text(encoding="utf-8")
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    version = project["project"]["version"]
    assert version == API_VERSION == PACKAGE_VERSION
    assert frontend["version"] == version
    assert frontend_lock["version"] == version
    assert frontend_lock["packages"][""]["version"] == version
    assert f"TESTOPS_IMAGE_TAG={version}" in environment
    assert set(re.findall(r"TESTOPS_IMAGE_TAG:-([0-9]+\.[0-9]+\.[0-9]+)", compose)) == {version}


def test_release_has_one_migration_head_and_required_operations_evidence() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "infra/alembic"))
    assert ScriptDirectory.from_config(config).get_heads() == ["20260821_0019"]

    required = (
        ROOT / ".github/workflows/ci.yml",
        ROOT / "infra/observability/prometheus.yml",
        ROOT / "infra/observability/alerts.yml",
        ROOT / "docs/operations/production-runbook.md",
        ROOT / "docs/operations/release-checklist.md",
        ROOT / "docs/milestones/M6-release-hardening.md",
        ROOT / "docs/milestones/M7.1-execution-quotas.md",
        ROOT / "docs/milestones/M7.2-runner-pools.md",
        ROOT / "docs/milestones/M7.3-regression-schedules.md",
        ROOT / "docs/milestones/M7.4-run-reliability.md",
        ROOT / "docs/milestones/M7.5-quality-analytics.md",
        ROOT / "docs/milestones/M7.6-quality-dimensions.md",
        ROOT / "docs/milestones/M7.7-flaky-case-detection.md",
        ROOT / "docs/milestones/M8.1-quality-change-alerts.md",
        ROOT / "docs/milestones/M8.2-quality-webhook-delivery.md",
        ROOT / "docs/milestones/M8.3-quality-alert-automation.md",
        ROOT / "docs/milestones/M8.4-quality-alert-operations.md",
        ROOT / "docs/milestones/M8.5-quality-webhook-replay.md",
        ROOT / "docs/milestones/M8.6-quality-operations-observability.md",
        ROOT / "docs/milestones/M9.1-automation-package-lifecycle.md",
        ROOT / "docs/milestones/M9.2-immutable-package-runtime-admission.md",
        ROOT / "docs/milestones/M9.3-automation-package-workbench.md",
        ROOT / "docs/milestones/M9.4.1-supply-chain-admission.md",
        ROOT / "docs/milestones/M9.4.2-signed-verifier-envelopes.md",
        ROOT / "docs/milestones/M9.5.1-subprocess-execution-isolation.md",
        ROOT / "docs/milestones/M9.5.2-container-execution-isolation.md",
        ROOT / "docs/milestones/M9.5.3-kubernetes-job-isolation.md",
        ROOT / "docs/milestones/M9.6.1-asymmetric-verifier-identity.md",
        ROOT / "infra/kubernetes/m9.5.3-runner.yaml",
        ROOT / "scripts/submit_supply_chain_verification.py",
    )
    assert all(path.is_file() and path.stat().st_size > 0 for path in required)
