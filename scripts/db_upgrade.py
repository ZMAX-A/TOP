"""Upgrade the configured control-plane database to the latest revision."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api/src"))
sys.path.insert(0, str(ROOT / "packages/contracts/src"))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402


def main() -> int:
    config = Config(str(ROOT / "alembic.ini"))
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
