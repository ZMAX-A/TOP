"""Process due project regression schedules into immutable Runs."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for relative in ("packages/contracts/src", "apps/api/src"):
    sys.path.insert(0, str(ROOT / relative))

from testops.api.config import Settings  # noqa: E402
from testops.api.database import create_database_runtime  # noqa: E402
from testops.api.schedule_services import process_due_schedules  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="schedule-regressions")
    parser.add_argument("--watch", action="store_true", help="continuously process due schedules")
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=5.0,
        help="poll interval used with --watch (default: 5.0)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="maximum schedules processed per poll (default: 50)",
    )
    return parser


async def run(
    *,
    watch: bool = False,
    interval_seconds: float = 5.0,
    batch_size: int = 50,
) -> dict[str, int]:
    if interval_seconds <= 0:
        raise ValueError("interval-seconds must be greater than zero")
    if not 1 <= batch_size <= 500:
        raise ValueError("batch-size must be between 1 and 500")
    settings = Settings.from_environment()
    engine, session_factory = create_database_runtime(settings)
    try:
        while True:
            summary = await process_due_schedules(session_factory, limit=batch_size)
            payload = {
                "selected": summary.selected,
                "triggered": summary.triggered,
                "skipped": summary.skipped,
                "blocked": summary.blocked,
                "failed": summary.failed,
            }
            print(json.dumps(payload, ensure_ascii=False), flush=True)
            if not watch:
                return payload
            await asyncio.sleep(interval_seconds)
    finally:
        await engine.dispose()


def main() -> int:
    args = _parser().parse_args()
    try:
        summary = asyncio.run(
            run(
                watch=args.watch,
                interval_seconds=args.interval_seconds,
                batch_size=args.batch_size,
            )
        )
    except KeyboardInterrupt:
        return 0
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
