"""Command line entrypoint for deterministic legacy case migration."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .legacy_excel import DEFAULT_WORKSHEET, migrate_legacy_excel, write_migration_result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="testops-migrate-legacy-excel")
    parser.add_argument("--source", required=True, help="read-only source .xlsx path")
    parser.add_argument("--project-key", required=True)
    parser.add_argument("--version", required=True, help="for example case-v1.0.0")
    parser.add_argument("--worksheet", default=DEFAULT_WORKSHEET)
    parser.add_argument("--output", required=True, help="immutable baseline output directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = migrate_legacy_excel(
        args.source,
        project_key=args.project_key,
        version=args.version,
        worksheet=args.worksheet,
    )
    paths = write_migration_result(result, args.output)
    summary = {
        "baseline_id": str(result.baseline.baseline_id),
        "version": result.baseline.version,
        "case_count": len(result.baseline.cases),
        "enabled_case_count": sum(case.enabled for case in result.baseline.cases),
        "output": str(Path(args.output).resolve()),
        "files": {key: path.name for key, path in paths.items()},
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
