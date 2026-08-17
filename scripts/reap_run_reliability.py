"""Recover timed-out Runs and stale Runner slot leases."""

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
from testops.api.reliability_services import process_run_reliability  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reap-run-reliability")
    parser.add_argument("--watch", action="store_true", help="continuously recover stalled Runs")
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=15.0,
        help="poll interval used with --watch (default: 15.0)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="maximum records processed per poll (default: 100)",
    )
    return parser


async def run(
    *,
    watch: bool = False,
    interval_seconds: float = 15.0,
    batch_size: int = 100,
) -> dict[str, int]:
    if interval_seconds <= 0:
        raise ValueError("interval-seconds must be greater than zero")
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch-size must be between 1 and 1000")
    settings = Settings.from_environment()
    engine, session_factory = create_database_runtime(settings)
    try:
        while True:
            summary = await process_run_reliability(
                session_factory,
                batch_size=batch_size,
                heartbeat_ttl_seconds=settings.runner_heartbeat_ttl_seconds,
                dispatch_start_timeout_seconds=(settings.run_dispatch_start_timeout_seconds),
            )
            payload = {
                "selected": summary.selected,
                "timed_out": summary.timed_out,
                "runner_lost": summary.runner_lost,
                "dispatch_stalled": summary.dispatch_stalled,
                "leases_released": summary.leases_released,
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
        asyncio.run(
            run(
                watch=args.watch,
                interval_seconds=args.interval_seconds,
                batch_size=args.batch_size,
            )
        )
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
