"""Verify immutable baselines, manifests and checked-in example jobs."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for relative in ("packages/contracts/src", "runners/web_playwright/src"):
    sys.path.insert(0, str(ROOT / relative))

from testops.contracts import CaseBaseline  # noqa: E402
from testops.runners.web import load_run_snapshot  # noqa: E402


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _runner_digest() -> str:
    source_root = ROOT / "runners/web_playwright/src"
    digest = hashlib.sha256()
    for path in sorted(source_root.rglob("*.py")):
        digest.update(path.relative_to(source_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def main() -> int:
    verified_baselines: dict[str, dict[str, object]] = {}
    baseline_root = ROOT / "baselines/yanjia-ai-web"
    for directory in sorted(path for path in baseline_root.iterdir() if path.is_dir()):
        baseline_bytes = (directory / "case-baseline.json").read_bytes()
        audit_bytes = (directory / "migration-audit.json").read_bytes()
        manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
        baseline = CaseBaseline.model_validate_json(baseline_bytes)
        if _digest(baseline_bytes) != manifest["baseline"]["digest"]:
            raise RuntimeError(f"baseline digest mismatch: {directory.name}")
        if _digest(audit_bytes) != manifest["audit"]["digest"]:
            raise RuntimeError(f"audit digest mismatch: {directory.name}")
        if len(baseline.cases) != manifest["baseline"]["case_count"]:
            raise RuntimeError(f"case count mismatch: {directory.name}")
        if (
            sum(case.enabled for case in baseline.cases)
            != manifest["baseline"]["enabled_case_count"]
        ):
            raise RuntimeError(f"enabled case count mismatch: {directory.name}")
        verified_baselines[baseline.version] = {
            "baseline_id": str(baseline.baseline_id),
            "digest": manifest["baseline"]["digest"],
            "case_count": len(baseline.cases),
            "enabled_case_count": sum(case.enabled for case in baseline.cases),
        }

    for directory in sorted(path for path in baseline_root.iterdir() if path.is_dir()):
        baseline = CaseBaseline.model_validate_json((directory / "case-baseline.json").read_bytes())
        if baseline.source.kind != "derived_baseline":
            continue
        parent = verified_baselines.get(baseline.source.parent_version)
        if parent is None or parent["digest"] != baseline.source.parent_digest:
            raise RuntimeError(f"derived parent reference mismatch: {directory.name}")
        if parent["baseline_id"] != str(baseline.source.parent_baseline_id):
            raise RuntimeError(f"derived parent ID mismatch: {directory.name}")

    verified_jobs: list[dict[str, object]] = []
    current_runner_digest = _runner_digest()
    for path in sorted((ROOT / "examples/jobs").glob("*.json")):
        job = load_run_snapshot(path)
        baseline = verified_baselines.get(job.case_baseline.version)
        if baseline is None:
            raise RuntimeError(f"job references unknown baseline: {path.name}")
        if baseline["digest"] != job.case_baseline.digest:
            raise RuntimeError(f"job baseline digest mismatch: {path.name}")
        if baseline["baseline_id"] != str(job.case_baseline.baseline_id):
            raise RuntimeError(f"job baseline ID mismatch: {path.name}")
        if job.automation_package.digest != current_runner_digest:
            raise RuntimeError(f"job Runner digest mismatch: {path.name}")
        verified_jobs.append(
            {
                "file": path.name,
                "run_id": str(job.run_id),
                "case_count": len(job.cases),
                "baseline_version": job.case_baseline.version,
            }
        )

    print(
        json.dumps(
            {
                "status": "passed",
                "baselines": verified_baselines,
                "jobs": verified_jobs,
                "runner_digest": current_runner_digest,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
