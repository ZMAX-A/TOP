"""Environment-backed settings for the TestOps control plane."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_DATABASE_URL = "postgresql+asyncpg://testops:change-me-local-only@localhost:5432/testops"


def _environment_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = DEFAULT_DATABASE_URL
    database_echo: bool = False
    auto_create_schema: bool = False
    runner_callback_token: str | None = field(default=None, repr=False)
    bootstrap_admin_token: str | None = field(default=None, repr=False)
    metrics_token: str | None = field(default=None, repr=False)
    session_ttl_hours: int = 8
    cors_origins: tuple[str, ...] = ("http://127.0.0.1:5173", "http://localhost:5173")
    minio_endpoint: str | None = None
    minio_access_key: str | None = None
    minio_secret_key: str | None = field(default=None, repr=False)
    minio_bucket: str = "testops-artifacts"
    minio_region: str = "us-east-1"
    artifact_url_ttl_seconds: int = 300
    run_event_poll_seconds: float = 0.5
    run_event_heartbeat_seconds: float = 15.0
    runner_heartbeat_ttl_seconds: int = 45
    run_dispatch_start_timeout_seconds: int = 300

    @classmethod
    def from_environment(cls) -> Settings:
        session_ttl_hours = int(os.getenv("SESSION_TTL_HOURS", "8"))
        if not 1 <= session_ttl_hours <= 24 * 30:
            raise ValueError("SESSION_TTL_HOURS must be between 1 and 720")
        artifact_url_ttl_seconds = int(os.getenv("ARTIFACT_URL_TTL_SECONDS", "300"))
        if not 30 <= artifact_url_ttl_seconds <= 3600:
            raise ValueError("ARTIFACT_URL_TTL_SECONDS must be between 30 and 3600")
        run_event_poll_seconds = float(os.getenv("RUN_EVENT_POLL_SECONDS", "0.5"))
        if not 0.1 <= run_event_poll_seconds <= 10:
            raise ValueError("RUN_EVENT_POLL_SECONDS must be between 0.1 and 10")
        run_event_heartbeat_seconds = float(os.getenv("RUN_EVENT_HEARTBEAT_SECONDS", "15"))
        if not 5 <= run_event_heartbeat_seconds <= 60:
            raise ValueError("RUN_EVENT_HEARTBEAT_SECONDS must be between 5 and 60")
        runner_heartbeat_ttl_seconds = int(os.getenv("RUNNER_HEARTBEAT_TTL_SECONDS", "45"))
        if not 15 <= runner_heartbeat_ttl_seconds <= 600:
            raise ValueError("RUNNER_HEARTBEAT_TTL_SECONDS must be between 15 and 600")
        run_dispatch_start_timeout_seconds = int(
            os.getenv("RUN_DISPATCH_START_TIMEOUT_SECONDS", "300")
        )
        if not 30 <= run_dispatch_start_timeout_seconds <= 3600:
            raise ValueError("RUN_DISPATCH_START_TIMEOUT_SECONDS must be between 30 and 3600")
        cors_origins = tuple(
            origin.strip()
            for origin in os.getenv(
                "CORS_ORIGINS",
                "http://127.0.0.1:5173,http://localhost:5173",
            ).split(",")
            if origin.strip()
        )
        return cls(
            database_url=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL),
            database_echo=_environment_bool("DATABASE_ECHO", False),
            auto_create_schema=_environment_bool("AUTO_CREATE_SCHEMA", False),
            runner_callback_token=os.getenv("RUNNER_CALLBACK_TOKEN") or None,
            bootstrap_admin_token=os.getenv("BOOTSTRAP_ADMIN_TOKEN") or None,
            metrics_token=os.getenv("METRICS_TOKEN") or None,
            session_ttl_hours=session_ttl_hours,
            cors_origins=cors_origins,
            minio_endpoint=os.getenv("MINIO_PUBLIC_ENDPOINT")
            or os.getenv("MINIO_ENDPOINT")
            or None,
            minio_access_key=os.getenv("MINIO_ACCESS_KEY") or os.getenv("MINIO_ROOT_USER") or None,
            minio_secret_key=os.getenv("MINIO_SECRET_KEY")
            or os.getenv("MINIO_ROOT_PASSWORD")
            or None,
            minio_bucket=os.getenv("MINIO_BUCKET", "testops-artifacts"),
            minio_region=os.getenv("MINIO_REGION", "us-east-1"),
            artifact_url_ttl_seconds=artifact_url_ttl_seconds,
            run_event_poll_seconds=run_event_poll_seconds,
            run_event_heartbeat_seconds=run_event_heartbeat_seconds,
            runner_heartbeat_ttl_seconds=runner_heartbeat_ttl_seconds,
            run_dispatch_start_timeout_seconds=run_dispatch_start_timeout_seconds,
        )
