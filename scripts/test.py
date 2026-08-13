"""Dependency-light unittest runner for all TestOps source packages."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "packages/contracts/src",
    "apps/api/src",
    "runners/web_playwright/src",
    "packages/migrations/src",
    "services/worker/src",
):
    sys.path.insert(0, str(ROOT / relative))


def main() -> int:
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
