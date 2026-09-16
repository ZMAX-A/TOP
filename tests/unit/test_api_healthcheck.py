from __future__ import annotations

import io
import json

import pytest
from scripts.check_api_health import (
    AI_REQUIRED_PATHS,
    CORE_REQUIRED_PATHS,
    ApiHealthError,
    check_api_health,
)


class JsonResponse(io.BytesIO):
    def __enter__(self) -> JsonResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def opener_for(documents: dict[str, object]):
    def open_url(url: str, *, timeout: float) -> JsonResponse:
        assert timeout == 3.0
        return JsonResponse(json.dumps(documents[url]).encode("utf-8"))

    return open_url


def test_ai_disabled_still_requires_core_feature_routes() -> None:
    base = "http://api:8000"
    check_api_health(
        base,
        ai_enabled=False,
        opener=opener_for(
            {
                f"{base}/readyz": {"status": "ready", "database": True},
                f"{base}/openapi.json": {"paths": {path: {} for path in CORE_REQUIRED_PATHS}},
            }
        ),
    )


def test_ai_enabled_requires_complete_ai_route_contract() -> None:
    base = "http://api:8000"
    documents = {
        f"{base}/readyz": {"status": "ready", "database": True},
        f"{base}/openapi.json": {
            "paths": {path: {} for path in AI_REQUIRED_PATHS | CORE_REQUIRED_PATHS}
        },
    }
    check_api_health(base, ai_enabled=True, opener=opener_for(documents))


def test_ai_enabled_rejects_api_image_without_ai_routes() -> None:
    base = "http://api:8000"
    documents = {
        f"{base}/readyz": {"status": "ready", "database": True},
        f"{base}/openapi.json": {"paths": {"/api/v1/projects": {}}},
    }
    with pytest.raises(ApiHealthError, match="missing required feature routes"):
        check_api_health(base, ai_enabled=True, opener=opener_for(documents))
