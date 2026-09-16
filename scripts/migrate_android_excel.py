"""Create an immutable TOP baseline from the YanJia Android workbook."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for relative in ("packages/contracts/src", "packages/migrations/src"):
    sys.path.insert(0, str(ROOT / relative))

from testops.migrations.android_excel import (  # noqa: E402
    DEFAULT_CASE_WORKSHEET,
    DEFAULT_STEP_WORKSHEET,
    migrate_android_excel,
    write_migration_result,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate YanJia Android Excel cases")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--project-key", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--worksheet", default=DEFAULT_CASE_WORKSHEET)
    parser.add_argument("--step-worksheet", default=DEFAULT_STEP_WORKSHEET)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = migrate_android_excel(
        args.source,
        project_key=args.project_key,
        version=args.version,
        worksheet=args.worksheet,
        step_worksheet=args.step_worksheet,
    )
    paths = write_migration_result(result, args.output)
    print(
        json.dumps(
            {
                "baseline_id": str(result.baseline.baseline_id),
                "version": result.baseline.version,
                "case_count": len(result.baseline.cases),
                "enabled_case_count": sum(case.enabled for case in result.baseline.cases),
                "output": str(args.output.resolve()),
                "files": {name: path.name for name, path in paths.items()},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
