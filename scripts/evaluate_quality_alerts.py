"""Evaluate project quality signals and enqueue transition Webhooks."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for relative in ("packages/contracts/src", "apps/api/src"):
    sys.path.insert(0, str(ROOT / relative))

from testops.api.config import Settings  # noqa: E402
from testops.api.database import create_database_runtime  # noqa: E402
from testops.api.quality_alert_services import evaluate_quality_alert_batch  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evaluate-quality-alerts")
    parser.add_argument("--watch", action="store_true", help="continuously evaluate due projects")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.getenv("QUALITY_ALERT_POLL_SECONDS", "5")),
        help="due-project poll interval used with --watch (default: 5)",
    )
    parser.add_argument(
        "--evaluation-interval-seconds",
        type=int,
        default=int(os.getenv("QUALITY_ALERT_EVALUATION_INTERVAL_SECONDS", "60")),
        help="minimum interval between project evaluations (default: 60)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.getenv("QUALITY_ALERT_BATCH_SIZE", "50")),
        help="maximum due projects evaluated per poll (default: 50)",
    )
    return parser


async def run(
    *,
    watch: bool = False,
    poll_seconds: float = 5.0,
    evaluation_interval_seconds: int = 60,
    batch_size: int = 50,
) -> dict[str, int]:
    if not 0.5 <= poll_seconds <= 60:
        raise ValueError("poll-seconds must be between 0.5 and 60")
    settings = Settings.from_environment()
    engine, session_factory = create_database_runtime(settings)
    latest = {
        "selected": 0,
        "evaluated": 0,
        "transitions": 0,
        "queued": 0,
        "cooldown_suppressed": 0,
        "silence_suppressed": 0,
    }
    try:
        while True:
            summary = await evaluate_quality_alert_batch(
                session_factory,
                batch_size=batch_size,
                evaluation_interval_seconds=evaluation_interval_seconds,
            )
            latest = {
                "selected": summary.selected,
                "evaluated": summary.evaluated,
                "transitions": summary.transitions,
                "queued": summary.queued,
                "cooldown_suppressed": summary.cooldown_suppressed,
                "silence_suppressed": summary.silence_suppressed,
            }
            if summary.selected:
                print(json.dumps(latest, ensure_ascii=False), flush=True)
            if not watch:
                return latest
            await asyncio.sleep(poll_seconds)
    finally:
        await engine.dispose()


def main() -> int:
    args = _parser().parse_args()
    try:
        asyncio.run(
            run(
                watch=args.watch,
                poll_seconds=args.poll_seconds,
                evaluation_interval_seconds=args.evaluation_interval_seconds,
                batch_size=args.batch_size,
            )
        )
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
