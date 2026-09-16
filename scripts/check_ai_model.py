"""Safely probe the configured OpenAI-compatible TestOps AI model."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api/src"))

from testops.api.ai_gateway import AiGatewayError, OpenAiCompatibleGateway  # noqa: E402
from testops.api.config import Settings  # noqa: E402


async def _main() -> int:
    try:
        settings = Settings.from_environment()
        gateway = OpenAiCompatibleGateway.from_settings(settings)
        if gateway is None:
            raise AiGatewayError("AI integration is disabled")
        result = await gateway.probe()
    except (AiGatewayError, ValueError) as exc:
        print(json.dumps({"status": "ERROR", "detail": str(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {"status": "READY", "model": result.model, "latency_ms": result.latency_ms},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
