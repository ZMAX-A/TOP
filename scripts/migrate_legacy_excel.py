"""Run the legacy Excel migration without installing component packages."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for relative in ("packages/contracts/src", "packages/migrations/src"):
    sys.path.insert(0, str(ROOT / relative))

from testops.migrations.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
