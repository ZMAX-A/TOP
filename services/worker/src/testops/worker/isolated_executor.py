"""Credential-minimized child process for one immutable Run Snapshot."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from testops.contracts import RunExecutionIsolationEvidence, RunResult, RunSnapshot
from testops.runners.legacy_pytest import LegacyPytestAdapter
from testops.runners.web import EnvironmentSecretProvider, PlaywrightWebAdapter

from .adapter_factory import create_automation_adapter

MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024
PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4
CORE_PATTERN_PATH = Path("/proc/sys/kernel/core_pattern")


def disable_process_crash_dumps() -> None:
    """Disable Linux core collection for this executor and all descendants.

    Linux ignores ``RLIMIT_CORE`` for piped ``core_pattern`` handlers, and WSL's
    crash collector has been observed to run even after ``PR_SET_DUMPABLE=0``.
    Reject that unsafe host state before launching browser children. With the
    WSL collector disabled, the zero core limit and non-dumpable state provide
    inherited defense in depth for Pytest, Playwright, and Chromium.
    """

    if not sys.platform.startswith("linux"):
        return

    core_pattern = CORE_PATTERN_PATH.read_text(encoding="utf-8").strip()
    if core_pattern.lstrip().startswith("|/wsl-capture-crash"):
        raise RuntimeError(
            "WSL crash collector is enabled; set maxCrashDumpCount=-1 and restart WSL"
        )

    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    if libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise RuntimeError("isolated executor remains dumpable")


def virtual_display_prefix(job: RunSnapshot) -> tuple[str, ...]:
    config = job.web_config
    if config is None or config.headless:
        return ()
    screen_width = max(1280, config.viewport_width)
    screen_height = max(720, config.viewport_height)
    return (
        "xvfb-run",
        "--auto-servernum",
        f"--server-args=-screen 0 {screen_width}x{screen_height}x24 -nolisten tcp",
    )


def subprocess_isolation_evidence(executor_version: str) -> RunExecutionIsolationEvidence:
    return RunExecutionIsolationEvidence(
        mode="SUBPROCESS",
        executor_version=executor_version,
        dedicated_process=True,
        credential_scope="RUN_SECRETS_ONLY",
        read_only_root_filesystem=False,
        network_policy="WORKER_DEFAULT",
        resource_limits_enforced=False,
    )


def isolation_evidence_from_environment(
    environ: Mapping[str, str] | None = None,
) -> RunExecutionIsolationEvidence:
    source = os.environ if environ is None else environ
    mode = source.get("TESTOPS_EXECUTOR_MODE", "SUBPROCESS")
    version = source.get("TESTOPS_EXECUTOR_VERSION", "unknown")
    if mode == "SUBPROCESS":
        return subprocess_isolation_evidence(version)
    if mode not in {"CONTAINER", "KUBERNETES"}:
        raise RuntimeError("unsupported isolated executor mode")
    if mode == "KUBERNETES":
        return RunExecutionIsolationEvidence(
            mode="KUBERNETES",
            executor_version=version,
            dedicated_process=True,
            credential_scope="RUN_SECRETS_ONLY",
            read_only_root_filesystem=True,
            network_policy=source.get("TESTOPS_NETWORK_POLICY", "WORKER_DEFAULT"),
            resource_limits_enforced=True,
            runtime_image_id=source.get("TESTOPS_RUNTIME_IMAGE_ID"),
            memory_limit_bytes=int(source.get("TESTOPS_MEMORY_LIMIT_BYTES", "0")),
            cpu_limit_millis=int(source.get("TESTOPS_CPU_LIMIT_MILLIS", "0")),
            ephemeral_storage_limit_bytes=int(
                source.get("TESTOPS_EPHEMERAL_STORAGE_LIMIT_BYTES", "0")
            ),
            orchestrator_namespace=source.get("TESTOPS_ORCHESTRATOR_NAMESPACE"),
            service_account_name=source.get("TESTOPS_SERVICE_ACCOUNT_NAME"),
            service_account_token_automounted=(
                source.get("TESTOPS_SERVICE_ACCOUNT_TOKEN_AUTOMOUNTED", "true").lower() == "true"
            ),
        )
    return RunExecutionIsolationEvidence(
        mode="CONTAINER",
        executor_version=version,
        dedicated_process=True,
        credential_scope="RUN_SECRETS_ONLY",
        read_only_root_filesystem=True,
        network_policy=source.get("TESTOPS_NETWORK_POLICY", "WORKER_DEFAULT"),
        resource_limits_enforced=True,
        runtime_image_id=source.get("TESTOPS_RUNTIME_IMAGE_ID"),
        memory_limit_bytes=int(source.get("TESTOPS_MEMORY_LIMIT_BYTES", "0")),
        cpu_limit_millis=int(source.get("TESTOPS_CPU_LIMIT_MILLIS", "0")),
        pids_limit=int(source.get("TESTOPS_PIDS_LIMIT", "0")),
    )


def persist_run_result(
    workspace_root: str | Path,
    result: RunResult,
    *,
    require_existing: bool = False,
) -> None:
    run_root = (Path(workspace_root).resolve() / str(result.run_id)).resolve()
    workspace = Path(workspace_root).resolve()
    if workspace not in run_root.parents:
        raise RuntimeError("isolated executor result escaped the workspace root")
    destination = run_root / "run-result.json"
    if require_existing and not destination.is_file():
        raise RuntimeError("isolated executor did not produce run-result.json")
    run_root.mkdir(parents=True, exist_ok=True)
    temporary = run_root / "run-result.enriched.tmp"
    temporary.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def execute_isolated_snapshot(
    snapshot_payload: Mapping[str, object],
    *,
    workspace_root: str | Path,
    environ: Mapping[str, str] | None = None,
    executor_version: str = "unknown",
    isolation_evidence: RunExecutionIsolationEvidence | None = None,
) -> RunResult:
    job = RunSnapshot.model_validate(snapshot_payload)
    child_environment = os.environ if environ is None else environ
    adapter = create_automation_adapter(
        job,
        workspace_root=workspace_root,
        secret_provider=EnvironmentSecretProvider(child_environment),
        environ=child_environment,
        playwright_adapter_type=PlaywrightWebAdapter,
        legacy_adapter_type=LegacyPytestAdapter,
    )
    adapter.prepare(job)
    result = adapter.execute(job, lambda _event: None).model_copy(
        update={
            "execution_isolation": isolation_evidence
            or subprocess_isolation_evidence(executor_version)
        }
    )
    persist_run_result(workspace_root, result, require_existing=True)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute one isolated TestOps Run Snapshot")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--input-file")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        disable_process_crash_dumps()
    except Exception:
        # Fail closed before loading any Run data or launching browser children.
        return 2
    args = _parser().parse_args(argv)
    try:
        payload = (
            Path(args.input_file).read_bytes()
            if args.input_file
            else sys.stdin.buffer.read(MAX_SNAPSHOT_BYTES + 1)
        )
    except OSError:
        return 2
    if not payload or len(payload) > MAX_SNAPSHOT_BYTES:
        return 2
    try:
        document = json.loads(payload)
        if not isinstance(document, dict):
            return 2
        execute_isolated_snapshot(
            document,
            workspace_root=args.workspace_root,
            executor_version=os.getenv("TESTOPS_EXECUTOR_VERSION", "unknown"),
            isolation_evidence=isolation_evidence_from_environment(),
        )
    except Exception:
        # Parent Worker reports a bounded generic infrastructure result. Child
        # exceptions may contain resolved case secrets and must not cross the boundary.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
