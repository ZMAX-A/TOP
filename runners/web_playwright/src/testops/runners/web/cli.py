"""Contract-first CLI for the Web Runner migration."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from testops.contracts import RunStatus

from .job_loader import load_run_snapshot
from .playwright_adapter import PlaywrightWebAdapter
from .variables import EnvironmentSecretProvider


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="testops-web-runner")
    subcommands = parser.add_subparsers(dest="command", required=True)
    validate = subcommands.add_parser("validate-job", help="validate an immutable Run Snapshot")
    validate.add_argument("path")
    execute = subcommands.add_parser("execute-job", help="execute a supported Run Snapshot")
    execute.add_argument("path")
    execute.add_argument("--workspace-root", default="artifacts/runs")
    subcommands.add_parser("health", help="report Runner dependency availability")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "validate-job":
        job = load_run_snapshot(args.path)
        safe_summary = {
            "run_id": str(job.run_id),
            "target_type": job.target_type.value,
            "case_count": len(job.cases),
            "case_baseline": {
                "baseline_id": str(job.case_baseline.baseline_id),
                "version": job.case_baseline.version,
                "digest": job.case_baseline.digest,
            },
            "automation_package": {
                "name": job.automation_package.name,
                "version": job.automation_package.version,
                "digest": job.automation_package.digest,
                "runner_type": job.automation_package.runner_type,
                "image_repository": job.automation_package.image_repository,
            },
            "secret_binding_count": len(job.secret_bindings),
        }
        print(json.dumps(safe_summary, ensure_ascii=False, indent=2))
        return 0
    if args.command == "health":
        adapter = PlaywrightWebAdapter(
            workspace_root="artifacts/runs",
            secret_provider=EnvironmentSecretProvider(),
        )
        print(json.dumps(adapter.health(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "execute-job":
        job = load_run_snapshot(args.path)
        adapter = PlaywrightWebAdapter(
            workspace_root=args.workspace_root,
            secret_provider=EnvironmentSecretProvider(),
        )
        adapter.prepare(job)
        result = adapter.execute(job, lambda _event: None)
        safe_summary = {
            "run_id": str(result.run_id),
            "status": result.status.value,
            "case_count": len(result.case_results),
            "artifact_count": len(result.artifacts),
            "workspace": str((Path(args.workspace_root) / str(result.run_id)).resolve()),
        }
        print(json.dumps(safe_summary, ensure_ascii=False, indent=2))
        return 0 if result.status == RunStatus.PASSED else 1
    return 2
