"""Publish one batch of pending control-plane Outbox events to Celery."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "packages/contracts/src",
    "apps/api/src",
    "services/worker/src",
):
    sys.path.insert(0, str(ROOT / relative))

from testops.api.config import Settings  # noqa: E402
from testops.api.database import create_database_runtime  # noqa: E402
from testops.worker.celery_app import celery_app  # noqa: E402
from testops.worker.config import WorkerSettings  # noqa: E402
from testops.worker.outbox import dispatch_outbox_batch  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dispatch-outbox")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="continuously poll and publish pending Outbox events",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=1.0,
        help="poll interval used with --watch (default: 1.0)",
    )
    return parser


async def run(*, watch: bool = False, interval_seconds: float = 1.0) -> dict[str, int]:
    if interval_seconds <= 0:
        raise ValueError("interval-seconds must be greater than zero")
    worker_settings = WorkerSettings.from_environment()
    api_settings = Settings(database_url=worker_settings.database_url)
    engine, session_factory = create_database_runtime(api_settings)
    try:
        while True:
            summary = await dispatch_outbox_batch(
                session_factory,
                celery_app,
                limit=worker_settings.outbox_batch_size,
                heartbeat_ttl_seconds=worker_settings.runner_heartbeat_ttl_seconds,
                capacity_poll_seconds=worker_settings.runner_capacity_poll_seconds,
            )
            payload = {
                "selected": summary.selected,
                "published": summary.published,
                "failed": summary.failed,
                "waiting": summary.waiting,
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
        summary = asyncio.run(run(watch=args.watch, interval_seconds=args.interval_seconds))
    except KeyboardInterrupt:
        return 0
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
