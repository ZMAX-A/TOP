"""Export checked-in JSON Schema artifacts from the Python source models."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/contracts/src"))

from testops.contracts.schema_export import schemas  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="export-schemas")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify checked-in schemas without modifying them",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    destination = ROOT / "packages/contracts/schemas"
    if not args.check:
        destination.mkdir(parents=True, exist_ok=True)
    for filename, schema in schemas().items():
        output = destination / filename
        payload = json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not output.is_file() or output.read_text("utf-8") != payload:
                raise RuntimeError(f"schema is out of date: {output.relative_to(ROOT)}")
        else:
            output.write_text(payload, encoding="utf-8")
        print(output.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
