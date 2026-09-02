"""Select the immutable automation-package adapter for a Run Snapshot."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from testops.contracts import RunSnapshot
from testops.runners.legacy_pytest import LEGACY_PACKAGE_NAME


def create_automation_adapter(
    job: RunSnapshot,
    *,
    workspace_root: str | Path,
    secret_provider: object,
    environ: Mapping[str, str] | None = None,
    playwright_adapter_type: Callable[..., Any],
    legacy_adapter_type: Callable[..., Any],
) -> object:
    """Dispatch by immutable package name while retaining one WEB runner type."""
    if job.automation_package.name == LEGACY_PACKAGE_NAME:
        source = os.environ if environ is None else environ
        project_root = source.get("TESTOPS_LEGACY_PYTEST_PROJECT_ROOT", "/opt/yanjia-web")
        return legacy_adapter_type(
            workspace_root=workspace_root,
            project_root=project_root,
            secret_provider=secret_provider,
            parent_environment=source,
        )
    return playwright_adapter_type(
        workspace_root=workspace_root,
        secret_provider=secret_provider,
    )
