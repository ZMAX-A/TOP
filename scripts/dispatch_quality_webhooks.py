"""Deliver pending project quality Webhooks with durable retry state."""

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
from testops.worker.quality_webhooks import (  # noqa: E402
    HttpxQualityWebhookSender,
    QualityWebhookDispatcherSettings,
    dispatch_quality_webhook_batch,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dispatch-quality-webhooks")
    parser.add_argument("--watch", action="store_true", help="continuously poll pending deliveries")
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=1.0,
        help="poll interval used with --watch (default: 1.0)",
    )
    return parser


async def run(*, watch: bool = False, interval_seconds: float = 1.0) -> dict[str, int]:
    if not 0.1 <= interval_seconds <= 60:
        raise ValueError("interval-seconds must be between 0.1 and 60")
    dispatcher_settings = QualityWebhookDispatcherSettings.from_environment()
    api_settings = Settings.from_environment()
    engine, session_factory = create_database_runtime(api_settings)
    sender = HttpxQualityWebhookSender(
        timeout_seconds=dispatcher_settings.timeout_seconds,
        allow_private_networks=dispatcher_settings.allow_private_networks,
    )
    latest = {"selected": 0, "delivered": 0, "retrying": 0, "failed": 0}
    try:
        while True:
            summary = await dispatch_quality_webhook_batch(
                session_factory,
                sender,
                limit=dispatcher_settings.batch_size,
                max_attempts=dispatcher_settings.max_attempts,
            )
            latest = {
                "selected": summary.selected,
                "delivered": summary.delivered,
                "retrying": summary.retrying,
                "failed": summary.failed,
            }
            if summary.selected:
                print(json.dumps(latest, ensure_ascii=False), flush=True)
            if not watch:
                return latest
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
