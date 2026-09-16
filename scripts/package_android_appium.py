"""Hash or export the allowlisted YanJia Android automation package."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "packages/contracts/src",
    "runners/web_playwright/src",
    "runners/android_appium/src",
):
    sys.path.insert(0, str(ROOT / relative))

from testops.runners.android_appium.package_digest import (  # noqa: E402
    export_package,
    package_source_digest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Hash or export YanJia Android automation")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--export-to", type=Path)
    args = parser.parse_args()
    digest = (
        export_package(args.source, args.export_to)
        if args.export_to
        else package_source_digest(args.source)
    )
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
