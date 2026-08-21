"""Credential-minimized child process for one immutable Run Snapshot."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from testops.contracts import RunExecutionIsolationEvidence, RunResult, RunSnapshot
from testops.runners.web import EnvironmentSecretProvider, PlaywrightWebAdapter

MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024


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
    adapter = PlaywrightWebAdapter(
        workspace_root=workspace_root,
        secret_provider=EnvironmentSecretProvider(child_environment),
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
