"""Select the immutable automation-package adapter for a Run Snapshot."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from testops.contracts import RunSnapshot
from testops.runners.android_appium import ANDROID_PACKAGE_NAME
from testops.runners.legacy_pytest import LEGACY_PACKAGE_NAME


def create_automation_adapter(
    job: RunSnapshot,
    *,
    workspace_root: str | Path,
    secret_provider: object,
    environ: Mapping[str, str] | None = None,
    playwright_adapter_type: Callable[..., Any],
    legacy_adapter_type: Callable[..., Any],
    android_adapter_type: Callable[..., Any] | None = None,
) -> object:
    """Dispatch by the immutable package name and declared runner type."""
    source = os.environ if environ is None else environ
    if (
        job.automation_package.name == ANDROID_PACKAGE_NAME
        or job.automation_package.runner_type == "ANDROID_APPIUM"
    ):
        if android_adapter_type is None:
            raise RuntimeError("Android Appium adapter is unavailable in this Worker")
        project_root = source.get("TESTOPS_ANDROID_APPIUM_PROJECT_ROOT", "")
        if not project_root:
            raise RuntimeError("TESTOPS_ANDROID_APPIUM_PROJECT_ROOT is required")
        return android_adapter_type(
            workspace_root=workspace_root,
            project_root=project_root,
            secret_provider=secret_provider,
            parent_environment=source,
        )
    if job.automation_package.name == LEGACY_PACKAGE_NAME:
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
