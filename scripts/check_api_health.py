"""Validate the live API readiness and enabled feature route contract."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from typing import Any
from urllib.request import urlopen

AI_REQUIRED_PATHS = frozenset(
    {
        "/api/v1/ai/status",
        "/api/v1/ai/models",
        "/api/v1/ai/providers",
        "/api/v1/ai/projects/{project_id}/chat",
        "/api/v1/ai/projects/{project_id}/chat/questions",
    }
)
CORE_REQUIRED_PATHS = frozenset(
    {
        "/api/v1/auth/me/run-email-notifications",
        "/api/v1/auth/me/run-email-notifications/test",
    }
)


class ApiHealthError(RuntimeError):
    """The running API does not satisfy its enabled feature contract."""


def _environment_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _json_response(
    url: str,
    *,
    timeout_seconds: float,
    opener: Callable[..., Any],
) -> dict[str, Any]:
    with opener(url, timeout=timeout_seconds) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ApiHealthError(f"{url} did not return a JSON object")
    return payload


def check_api_health(
    base_url: str,
    *,
    ai_enabled: bool,
    timeout_seconds: float = 3.0,
    opener: Callable[..., Any] = urlopen,
) -> None:
    base = base_url.rstrip("/")
    ready = _json_response(
        f"{base}/readyz",
        timeout_seconds=timeout_seconds,
        opener=opener,
    )
    if ready.get("status") != "ready" or ready.get("database") is not True:
        raise ApiHealthError(f"API readiness check failed: {ready}")

    openapi = _json_response(
        f"{base}/openapi.json",
        timeout_seconds=timeout_seconds,
        opener=opener,
    )
    paths = openapi.get("paths")
    if not isinstance(paths, dict):
        raise ApiHealthError("OpenAPI document does not contain a paths object")
    required_paths = CORE_REQUIRED_PATHS | (AI_REQUIRED_PATHS if ai_enabled else frozenset())
    missing = sorted(required_paths.difference(paths))
    if missing:
        raise ApiHealthError(
            "API image is missing required feature routes: " + ", ".join(missing)
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout-seconds", type=float, default=3.0)
    arguments = parser.parse_args()
    check_api_health(
        arguments.base_url,
        ai_enabled=_environment_bool("AI_ENABLED"),
        timeout_seconds=arguments.timeout_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
