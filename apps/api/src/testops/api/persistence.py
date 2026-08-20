"""Control-plane persistence model for the M2 vertical slice."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


def utc_now() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class SystemSettingRecord(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )


class UserRecord(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    system_role: Mapped[str] = mapped_column(String(32), nullable=False, default="USER")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")


class AuthSessionRecord(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProjectRecord(TimestampMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(
            "max_in_flight_runs BETWEEN 1 AND 500",
            name="max_in_flight_runs_range",
        ),
        CheckConstraint(
            "max_daily_runs BETWEEN 1 AND 100000",
            name="max_daily_runs_range",
        ),
        CheckConstraint(
            "run_timeout_seconds BETWEEN 60 AND 86400",
            name="run_timeout_seconds_range",
        ),
        CheckConstraint(
            "quality_slo_target_percent BETWEEN 1 AND 100",
            name="quality_slo_target_percent_range",
        ),
        CheckConstraint(
            "quality_slo_window_days BETWEEN 7 AND 90",
            name="quality_slo_window_days_range",
        ),
        CheckConstraint(
            "quality_alert_warning_drop_points BETWEEN 1 AND 100",
            name="quality_alert_warning_drop_points_range",
        ),
        CheckConstraint(
            "quality_alert_critical_drop_points BETWEEN 1 AND 100",
            name="quality_alert_critical_drop_points_range",
        ),
        CheckConstraint(
            "quality_alert_warning_drop_points < quality_alert_critical_drop_points",
            name="quality_alert_drop_points_order",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    max_in_flight_runs: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=20,
        server_default="20",
    )
    max_daily_runs: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=500,
        server_default="500",
    )
    run_timeout_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=3600,
        server_default="3600",
    )
    quality_slo_target_percent: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=95,
        server_default="95",
    )
    quality_slo_window_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=30,
        server_default="30",
    )
    quality_alert_warning_drop_points: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=5,
        server_default="5",
    )
    quality_alert_critical_drop_points: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=10,
        server_default="10",
    )


class ProjectMemberRecord(TimestampMixin, Base):
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "user_id"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class TestTargetRecord(TimestampMixin, Base):
    __tablename__ = "test_targets"
    __table_args__ = (UniqueConstraint("project_id", "key"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runner_pool_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("runner_pools.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    browser: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")


class EnvironmentRecord(TimestampMixin, Base):
    __tablename__ = "environments"
    __table_args__ = (UniqueConstraint("target_id", "key"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_id: Mapped[UUID] = mapped_column(
        ForeignKey("test_targets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runner_pool_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("runner_pools.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    config_document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")


class CaseBaselineRecord(TimestampMixin, Base):
    __tablename__ = "case_baselines"
    __table_args__ = (
        UniqueConstraint("project_id", "version"),
        UniqueConstraint("project_id", "digest"),
    )

    baseline_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    digest: Mapped[str] = mapped_column(String(71), nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled_case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RELEASED")
    document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)


class ChangeRequestRecord(TimestampMixin, Base):
    __tablename__ = "change_requests"
    __table_args__ = (UniqueConstraint("project_id", "candidate_version"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    base_baseline_id: Mapped[UUID] = mapped_column(
        ForeignKey("case_baselines.baseline_id", ondelete="RESTRICT"),
        nullable=False,
    )
    candidate_baseline_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_version: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    candidate_document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DRAFT")
    validation_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING_EXECUTION"
    )
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_baseline_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    validation_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("test_runs.id", ondelete="RESTRICT"),
        nullable=True,
    )


class ChangeItemRecord(Base):
    __tablename__ = "change_items"
    __table_args__ = (UniqueConstraint("change_request_id", "sequence"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    change_request_id: Mapped[UUID] = mapped_column(
        ForeignKey("change_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    change_type: Mapped[str] = mapped_column(String(16), nullable=False)
    case_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    case_code: Mapped[str] = mapped_column(String(64), nullable=False)
    before_document: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    after_document: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    changed_fields: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False)


class ApprovalRecord(Base):
    __tablename__ = "approvals"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    change_request_id: Mapped[UUID] = mapped_column(
        ForeignKey("change_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reviewer_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )


class AutomationPackageRecord(TimestampMixin, Base):
    __tablename__ = "automation_packages"
    __table_args__ = (
        UniqueConstraint("target_id", "name", "version"),
        CheckConstraint(
            "runner_type IN ('WEB_PLAYWRIGHT')",
            name="runner_type_allowed",
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'DEPRECATED', 'REVOKED')",
            name="status_allowed",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_id: Mapped[UUID] = mapped_column(
        ForeignKey("test_targets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    digest: Mapped[str] = mapped_column(String(71), nullable=False)
    runner_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="WEB_PLAYWRIGHT",
        server_default="WEB_PLAYWRIGHT",
    )
    image_repository: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        default="testops-worker",
        server_default="testops-worker",
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    supersedes_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("automation_packages.id", ondelete="RESTRICT"),
        index=True,
    )
    validated_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("test_runs.id", ondelete="SET NULL"),
        index=True,
    )
    activated_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_reason: Mapped[str | None] = mapped_column(String(500))


class RunnerPoolRecord(TimestampMixin, Base):
    __tablename__ = "runner_pools"
    __table_args__ = (
        CheckConstraint(
            "max_concurrency BETWEEN 1 AND 500",
            name="max_concurrency_range",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    target_types: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False)
    queue_name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")


class RunnerWorkerRecord(TimestampMixin, Base):
    __tablename__ = "runner_workers"
    __table_args__ = (
        CheckConstraint(
            "max_slots BETWEEN 1 AND 100",
            name="max_slots_range",
        ),
        UniqueConstraint("worker_key"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    pool_id: Mapped[UUID] = mapped_column(
        ForeignKey("runner_pools.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    worker_key: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    runner_version: Mapped[str] = mapped_column(String(64), nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    max_slots: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )


class RegressionScheduleRecord(TimestampMixin, Base):
    __tablename__ = "regression_schedules"
    __table_args__ = (
        UniqueConstraint("project_id", "key"),
        CheckConstraint(
            "misfire_grace_seconds BETWEEN 60 AND 86400",
            name="misfire_grace_seconds_range",
        ),
        Index("ix_regression_schedules_due", "status", "next_fire_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_id: Mapped[UUID] = mapped_column(
        ForeignKey("test_targets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    environment_id: Mapped[UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    baseline_id: Mapped[UUID] = mapped_column(
        ForeignKey("case_baselines.baseline_id", ondelete="RESTRICT"),
        nullable=False,
    )
    automation_package_id: Mapped[UUID] = mapped_column(
        ForeignKey("automation_packages.id", ondelete="RESTRICT"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    case_codes: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    cron_expression: Mapped[str] = mapped_column(String(128), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    misfire_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    misfire_grace_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("test_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )


class TestRunRecord(TimestampMixin, Base):
    __tablename__ = "test_runs"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key"),
        CheckConstraint(
            "timeout_seconds BETWEEN 60 AND 86400",
            name="timeout_seconds_range",
        ),
        Index("ix_test_runs_project_status_created", "project_id", "status", "created_at"),
        Index(
            "ix_test_runs_project_target_status_finished",
            "project_id",
            "target_id",
            "status",
            "finished_at",
        ),
        Index(
            "ix_test_runs_project_environment_status_finished",
            "project_id",
            "environment_id",
            "status",
            "finished_at",
        ),
        Index(
            "ix_test_runs_project_baseline_status_finished",
            "project_id",
            "baseline_id",
            "status",
            "finished_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_id: Mapped[UUID] = mapped_column(
        ForeignKey("test_targets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    environment_id: Mapped[UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    baseline_id: Mapped[UUID] = mapped_column(
        ForeignKey("case_baselines.baseline_id", ondelete="RESTRICT"),
        nullable=False,
    )
    automation_package_id: Mapped[UUID] = mapped_column(
        ForeignKey("automation_packages.id", ondelete="RESTRICT"),
        nullable=False,
    )
    runner_pool_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("runner_pools.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    regression_schedule_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("regression_schedules.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("test_runs.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    retry_mode: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="QUEUED")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    result_digest: Mapped[str | None] = mapped_column(String(71))
    result_document: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=3600,
        server_default="3600",
    )
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dispatch_state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="PENDING",
        server_default="PENDING",
    )
    dispatch_wait_reason: Mapped[str | None] = mapped_column(String(64))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timeout_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)


class RegressionScheduleFiringRecord(Base):
    __tablename__ = "regression_schedule_firings"
    __table_args__ = (
        UniqueConstraint("schedule_id", "scheduled_for"),
        Index("ix_regression_schedule_firings_schedule_created", "schedule_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    schedule_id: Mapped[UUID] = mapped_column(
        ForeignKey("regression_schedules.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("test_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trigger_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class RunnerSlotLeaseRecord(Base):
    __tablename__ = "runner_slot_leases"
    __table_args__ = (Index("ix_runner_slot_leases_pool_status", "pool_id", "status"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    pool_id: Mapped[UUID] = mapped_column(
        ForeignKey("runner_pools.id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("test_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    worker_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("runner_workers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunCaseRecord(TimestampMixin, Base):
    __tablename__ = "run_cases"
    __table_args__ = (
        UniqueConstraint("run_id", "case_id"),
        UniqueConstraint("run_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("test_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    case_code: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="QUEUED")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    failure_category: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)


class ArtifactRecord(Base):
    __tablename__ = "artifacts"

    artifact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("test_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    digest: Mapped[str] = mapped_column(String(71), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )


class RunEventRecord(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "dedupe_key", name="uq_run_events_run_id"),
        UniqueConstraint(
            "run_id",
            "sequence",
            name="uq_run_events_run_id_sequence",
        ),
        Index("ix_run_events_run_sequence", "run_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("test_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    case_code: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str | None] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )


class DispatchOutboxRecord(Base):
    __tablename__ = "dispatch_outbox"
    __table_args__ = (
        UniqueConstraint("dedupe_key"),
        Index("ix_dispatch_outbox_status_available", "status", "available_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class QualityWebhookConfigRecord(TimestampMixin, Base):
    __tablename__ = "quality_webhook_configs"
    __table_args__ = (
        UniqueConstraint("project_id"),
        CheckConstraint(
            "minimum_alert_status IN ('WARNING', 'CRITICAL')",
            name="minimum_alert_status_values",
        ),
        CheckConstraint(
            "(signing_secret_name IS NULL AND signing_secret_ref IS NULL) "
            "OR (signing_secret_name IS NOT NULL AND signing_secret_ref IS NOT NULL)",
            name="signing_secret_pair",
        ),
        CheckConstraint(
            "cooldown_seconds BETWEEN 60 AND 86400",
            name="cooldown_seconds_range",
        ),
        CheckConstraint(
            "(silenced_until IS NULL AND silenced_by IS NULL AND silence_reason IS NULL) "
            "OR (silenced_until IS NOT NULL AND silenced_by IS NOT NULL "
            "AND silence_reason IS NOT NULL)",
            name="silence_fields_consistent",
        ),
        Index(
            "ix_quality_webhook_configs_enabled_due",
            "enabled",
            "next_evaluation_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    endpoint_url: Mapped[str] = mapped_column(Text, nullable=False)
    minimum_alert_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="WARNING",
        server_default="WARNING",
    )
    signing_secret_name: Mapped[str | None] = mapped_column(String(100))
    signing_secret_ref: Mapped[str | None] = mapped_column(String(500))
    cooldown_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=3600,
        server_default="3600",
    )
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_evaluation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    silenced_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    silenced_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    silence_reason: Mapped[str | None] = mapped_column(String(500))


class QualityWebhookDeliveryRecord(Base):
    __tablename__ = "quality_webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("dedupe_key"),
        UniqueConstraint("replay_of_id"),
        CheckConstraint(
            "status IN ('PENDING', 'DELIVERED', 'FAILED')",
            name="status_values",
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(
            "response_status IS NULL OR response_status BETWEEN 100 AND 599",
            name="response_status_range",
        ),
        CheckConstraint(
            "(replay_of_id IS NULL AND replayed_by IS NULL AND replay_reason IS NULL) "
            "OR (replay_of_id IS NOT NULL AND replayed_by IS NOT NULL "
            "AND replay_reason IS NOT NULL)",
            name="replay_fields_consistent",
        ),
        Index(
            "ix_quality_webhook_deliveries_status_available",
            "status",
            "available_at",
        ),
        Index(
            "ix_quality_webhook_deliveries_project_created",
            "project_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    webhook_config_id: Mapped[UUID] = mapped_column(
        ForeignKey("quality_webhook_configs.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    destination_display: Mapped[str] = mapped_column(String(500), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    response_status: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    replay_of_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("quality_webhook_deliveries.id", ondelete="CASCADE")
    )
    replayed_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    replay_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QualityAlertStateRecord(TimestampMixin, Base):
    __tablename__ = "quality_alert_states"
    __table_args__ = (
        UniqueConstraint("project_id", "metric"),
        CheckConstraint(
            "metric IN ('RUN_PASS_RATE', 'CASE_PASS_RATE', 'EXECUTION_RELIABILITY')",
            name="metric_values",
        ),
        CheckConstraint(
            "current_status IN ('NO_DATA', 'STABLE', 'WARNING', 'CRITICAL')",
            name="current_status_values",
        ),
        CheckConstraint(
            "active_notification_status IS NULL "
            "OR active_notification_status IN ('WARNING', 'CRITICAL')",
            name="active_notification_status_values",
        ),
        CheckConstraint(
            "current_percent IS NULL OR current_percent BETWEEN 0 AND 100",
            name="current_percent_range",
        ),
        CheckConstraint(
            "previous_percent IS NULL OR previous_percent BETWEEN 0 AND 100",
            name="previous_percent_range",
        ),
        CheckConstraint(
            "delta_percentage_points IS NULL OR delta_percentage_points BETWEEN -100 AND 100",
            name="delta_percentage_points_range",
        ),
        CheckConstraint(
            "notification_sequence >= 0",
            name="notification_sequence_non_negative",
        ),
        CheckConstraint(
            "(acknowledged_at IS NULL AND acknowledged_by IS NULL "
            "AND acknowledgement_note IS NULL) "
            "OR (acknowledged_at IS NOT NULL AND acknowledged_by IS NOT NULL "
            "AND acknowledgement_note IS NOT NULL)",
            name="acknowledgement_fields_consistent",
        ),
        Index("ix_quality_alert_states_project_status", "project_id", "current_status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    current_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="NO_DATA",
    )
    active_notification_status: Mapped[str | None] = mapped_column(String(16))
    current_percent: Mapped[float | None] = mapped_column(Float)
    previous_percent: Mapped[float | None] = mapped_column(Float)
    delta_percentage_points: Mapped[float | None] = mapped_column(Float)
    signal_fingerprint: Mapped[str] = mapped_column(String(71), nullable=False)
    notification_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_transition_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_delivery_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("quality_webhook_deliveries.id", ondelete="SET NULL")
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    acknowledgement_note: Mapped[str | None] = mapped_column(String(500))


class AuditLogRecord(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_project_created", "project_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"),
        index=True,
    )
    actor_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
