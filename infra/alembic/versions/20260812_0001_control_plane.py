"""Create M2 control-plane tables.

Revision ID: 20260812_0001
Revises: None
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260812_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_DOCUMENT = postgresql.JSONB(astext_type=sa.Text())


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
        sa.UniqueConstraint("key", name="uq_projects_key"),
    )
    op.create_table(
        "test_targets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("target_type", sa.String(length=16), nullable=False),
        sa.Column("browser", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_test_targets_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_test_targets"),
        sa.UniqueConstraint("project_id", "key", name="uq_test_targets_project_id"),
    )
    op.create_index("ix_test_targets_project_id", "test_targets", ["project_id"])
    op.create_table(
        "environments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("config_document", JSON_DOCUMENT, nullable=False),
        sa.Column("config_hash", sa.String(length=71), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_environments_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["test_targets.id"],
            name="fk_environments_target_id_test_targets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_environments"),
        sa.UniqueConstraint("target_id", "key", name="uq_environments_target_id"),
    )
    op.create_index("ix_environments_project_id", "environments", ["project_id"])
    op.create_index("ix_environments_target_id", "environments", ["target_id"])
    op.create_table(
        "case_baselines",
        sa.Column("baseline_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("digest", sa.String(length=71), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("enabled_case_count", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("document", JSON_DOCUMENT, nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_case_baselines_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("baseline_id", name="pk_case_baselines"),
        sa.UniqueConstraint("project_id", "version", name="uq_case_baselines_project_id"),
        sa.UniqueConstraint("project_id", "digest", name="uq_case_baselines_project_id_digest"),
    )
    op.create_index("ix_case_baselines_project_id", "case_baselines", ["project_id"])
    op.create_table(
        "automation_packages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("digest", sa.String(length=71), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_automation_packages_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["test_targets.id"],
            name="fk_automation_packages_target_id_test_targets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_automation_packages"),
        sa.UniqueConstraint(
            "target_id", "name", "version", name="uq_automation_packages_target_id"
        ),
    )
    op.create_index("ix_automation_packages_project_id", "automation_packages", ["project_id"])
    op.create_index("ix_automation_packages_target_id", "automation_packages", ["target_id"])
    op.create_table(
        "test_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("baseline_id", sa.Uuid(), nullable=False),
        sa.Column("automation_package_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=71), nullable=False),
        sa.Column("snapshot_digest", sa.String(length=71), nullable=False),
        sa.Column("snapshot", JSON_DOCUMENT, nullable=False),
        sa.Column("result_digest", sa.String(length=71), nullable=True),
        sa.Column("result_document", JSON_DOCUMENT, nullable=True),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["automation_package_id"],
            ["automation_packages.id"],
            name="fk_test_runs_automation_package_id_automation_packages",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_id"],
            ["case_baselines.baseline_id"],
            name="fk_test_runs_baseline_id_case_baselines",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name="fk_test_runs_environment_id_environments",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_test_runs_project_id_projects",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["test_targets.id"],
            name="fk_test_runs_target_id_test_targets",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_test_runs"),
        sa.UniqueConstraint("project_id", "idempotency_key", name="uq_test_runs_project_id"),
    )
    op.create_index(
        "ix_test_runs_project_status_created", "test_runs", ["project_id", "status", "created_at"]
    )
    op.create_table(
        "run_cases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("case_code", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("failure_category", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["run_id"], ["test_runs.id"], name="fk_run_cases_run_id_test_runs", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_run_cases"),
        sa.UniqueConstraint("run_id", "case_id", name="uq_run_cases_run_id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_cases_run_id_sequence"),
    )
    op.create_index("ix_run_cases_run_id", "run_cases", ["run_id"])
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("digest", sa.String(length=71), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["test_runs.id"],
            name="fk_artifacts_run_id_test_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("artifact_id", name="pk_artifacts"),
    )
    op.create_index("ix_artifacts_run_id", "artifacts", ["run_id"])
    op.create_table(
        "dispatch_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_dispatch_outbox"),
        sa.UniqueConstraint("dedupe_key", name="uq_dispatch_outbox_dedupe_key"),
    )
    op.create_index(
        "ix_dispatch_outbox_status_available", "dispatch_outbox", ["status", "available_at"]
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("details", JSON_DOCUMENT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_audit_logs_project_id_projects",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
    )
    op.create_index("ix_audit_logs_project_id", "audit_logs", ["project_id"])
    op.create_index("ix_audit_logs_project_created", "audit_logs", ["project_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_project_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_project_id", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_dispatch_outbox_status_available", table_name="dispatch_outbox")
    op.drop_table("dispatch_outbox")
    op.drop_index("ix_artifacts_run_id", table_name="artifacts")
    op.drop_table("artifacts")
    op.drop_index("ix_run_cases_run_id", table_name="run_cases")
    op.drop_table("run_cases")
    op.drop_index("ix_test_runs_project_status_created", table_name="test_runs")
    op.drop_table("test_runs")
    op.drop_index("ix_automation_packages_target_id", table_name="automation_packages")
    op.drop_index("ix_automation_packages_project_id", table_name="automation_packages")
    op.drop_table("automation_packages")
    op.drop_index("ix_case_baselines_project_id", table_name="case_baselines")
    op.drop_table("case_baselines")
    op.drop_index("ix_environments_target_id", table_name="environments")
    op.drop_index("ix_environments_project_id", table_name="environments")
    op.drop_table("environments")
    op.drop_index("ix_test_targets_project_id", table_name="test_targets")
    op.drop_table("test_targets")
    op.drop_table("projects")
