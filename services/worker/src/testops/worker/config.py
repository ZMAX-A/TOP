"""Worker settings sourced from the process environment."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    database_url: str
    broker_url: str
    control_plane_url: str
    runner_callback_token: str = field(repr=False)
    workspace_root: str
    outbox_batch_size: int
    minio_endpoint: str | None
    minio_access_key: str | None = field(repr=False)
    minio_secret_key: str | None = field(repr=False)
    minio_bucket: str
    minio_region: str
    runner_worker_key: str | None
    runner_pool_key: str | None
    runner_display_name: str
    runner_version: str
    runner_max_slots: int
    runner_capabilities: dict[str, object]
    runner_heartbeat_interval_seconds: float
    runner_heartbeat_ttl_seconds: int
    runner_capacity_poll_seconds: float

    @classmethod
    def from_environment(cls) -> WorkerSettings:
        token = os.getenv("RUNNER_CALLBACK_TOKEN", "")
        if not token:
            raise RuntimeError("RUNNER_CALLBACK_TOKEN is required for the Worker")
        batch_size = int(os.getenv("OUTBOX_BATCH_SIZE", "20"))
        if not 1 <= batch_size <= 500:
            raise RuntimeError("OUTBOX_BATCH_SIZE must be between 1 and 500")
        worker_key = os.getenv("RUNNER_WORKER_KEY") or None
        pool_key = os.getenv("RUNNER_POOL_KEY") or None
        if (worker_key is None) != (pool_key is None):
            raise RuntimeError("RUNNER_WORKER_KEY and RUNNER_POOL_KEY must be configured together")
        max_slots = int(os.getenv("RUNNER_MAX_SLOTS", "1"))
        if not 1 <= max_slots <= 100:
            raise RuntimeError("RUNNER_MAX_SLOTS must be between 1 and 100")
        heartbeat_interval = float(os.getenv("RUNNER_HEARTBEAT_INTERVAL_SECONDS", "15"))
        if not 5 <= heartbeat_interval <= 300:
            raise RuntimeError("RUNNER_HEARTBEAT_INTERVAL_SECONDS must be between 5 and 300")
        heartbeat_ttl = int(os.getenv("RUNNER_HEARTBEAT_TTL_SECONDS", "45"))
        if not 15 <= heartbeat_ttl <= 600:
            raise RuntimeError("RUNNER_HEARTBEAT_TTL_SECONDS must be between 15 and 600")
        capacity_poll = float(os.getenv("RUNNER_CAPACITY_POLL_SECONDS", "2"))
        if not 0.1 <= capacity_poll <= 60:
            raise RuntimeError("RUNNER_CAPACITY_POLL_SECONDS must be between 0.1 and 60")
        try:
            capabilities = json.loads(
                os.getenv(
                    "RUNNER_CAPABILITIES",
                    '{"target_types":["WEB"],"browsers":["chromium"],"labels":{}}',
                )
            )
        except json.JSONDecodeError as exc:
            raise RuntimeError("RUNNER_CAPABILITIES must be valid JSON") from exc
        if not isinstance(capabilities, dict):
            raise RuntimeError("RUNNER_CAPABILITIES must be a JSON object")
        return cls(
            database_url=os.getenv(
                "DATABASE_URL",
                "postgresql+asyncpg://testops:change-me-local-only@localhost:5432/testops",
            ),
            broker_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            control_plane_url=os.getenv("CONTROL_PLANE_URL", "http://127.0.0.1:8000"),
            runner_callback_token=token,
            workspace_root=os.getenv("RUNNER_WORKSPACE_ROOT", "artifacts/runs"),
            outbox_batch_size=batch_size,
            minio_endpoint=os.getenv("MINIO_ENDPOINT") or None,
            minio_access_key=os.getenv("MINIO_ACCESS_KEY") or os.getenv("MINIO_ROOT_USER") or None,
            minio_secret_key=os.getenv("MINIO_SECRET_KEY")
            or os.getenv("MINIO_ROOT_PASSWORD")
            or None,
            minio_bucket=os.getenv("MINIO_BUCKET", "testops-artifacts"),
            minio_region=os.getenv("MINIO_REGION", "us-east-1"),
            runner_worker_key=worker_key,
            runner_pool_key=pool_key,
            runner_display_name=os.getenv("RUNNER_DISPLAY_NAME", worker_key or "Legacy Worker"),
            runner_version=os.getenv("RUNNER_VERSION", "0.22.0"),
            runner_max_slots=max_slots,
            runner_capabilities=capabilities,
            runner_heartbeat_interval_seconds=heartbeat_interval,
            runner_heartbeat_ttl_seconds=heartbeat_ttl,
            runner_capacity_poll_seconds=capacity_poll,
        )
