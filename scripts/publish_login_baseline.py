"""Publish case-v1.0.1 with a strict successful-login URL assertion."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for relative in ("packages/contracts/src", "packages/migrations/src"):
    sys.path.insert(0, str(ROOT / relative))

from testops.contracts import AssertionDefinition, CaseBaseline  # noqa: E402
from testops.migrations import derive_case_baseline, write_migration_result  # noqa: E402
from testops.migrations.legacy_excel import LegacyExcelMigrationError  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="publish-login-baseline")
    parser.add_argument(
        "--parent",
        default=str(ROOT / "baselines/yanjia-ai-web/case-v1.0.0"),
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "baselines/yanjia-ai-web/case-v1.0.1"),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    parent_directory = Path(args.parent)
    baseline_bytes = (parent_directory / "case-baseline.json").read_bytes()
    manifest = json.loads((parent_directory / "manifest.json").read_text("utf-8"))
    digest = f"sha256:{hashlib.sha256(baseline_bytes).hexdigest()}"
    if digest != manifest["baseline"]["digest"]:
        raise LegacyExcelMigrationError("父基线文件与 manifest 哈希不一致")

    parent = CaseBaseline.model_validate_json(baseline_bytes)
    original = next(case for case in parent.cases if case.case_code == "TC-LOGIN-007")
    replacement = AssertionDefinition(
        type="url_not_contains",
        expected="URL不包含'/login'",
        locator=original.assertion.locator,
        timeout_seconds=original.assertion.timeout_seconds,
    )
    result = derive_case_baseline(
        parent,
        parent_digest=digest,
        version="case-v1.0.1",
        change_set="harden-successful-login-url-assertion",
        assertion_updates={"TC-LOGIN-007": replacement},
    )
    paths = write_migration_result(result, args.output)
    summary = {
        "baseline_id": str(result.baseline.baseline_id),
        "version": result.baseline.version,
        "case_count": len(result.baseline.cases),
        "changed_cases": ["TC-LOGIN-007"],
        "files": {name: path.name for name, path in paths.items()},
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
