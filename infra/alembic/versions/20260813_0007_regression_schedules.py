"""Add timezone-aware regression schedules and firing history.

Revision ID: 20260813_0007
Revises: 20260813_0006
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260813_0007"
down_revision: str | None = "20260813_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_DOCUMENT = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "regression_schedules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("baseline_id", sa.Uuid(), nullable=False),
        sa.Column("automation_package_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("case_codes", JSON_DOCUMENT, nullable=False),
        sa.Column("cron_expression", sa.String(length=128), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("misfire_policy", sa.String(length=32), nullable=False),
        sa.Column("misfire_grace_seconds", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_id", sa.Uuid(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "misfire_grace_seconds BETWEEN 60 AND 86400",
            name="ck_regression_schedules_misfire_grace_seconds_range",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_regression_schedules_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["test_targets.id"],
            name="fk_regression_schedules_target_id_test_targets",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name="fk_regression_schedules_environment_id_environments",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_id"],
            ["case_baselines.baseline_id"],
            name="fk_regression_schedules_baseline_id_case_baselines",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["automation_package_id"],
            ["automation_packages.id"],
            name="fk_regression_schedules_package_id_automation_packages",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["last_run_id"],
            ["test_runs.id"],
            name="fk_regression_schedules_last_run_id_test_runs",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_regression_schedules_created_by_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_regression_schedules"),
        sa.UniqueConstraint(
            "project_id",
            "key",
            name="uq_regression_schedules_project_id",
        ),
    )
    op.create_index(
        "ix_regression_schedules_project_id",
        "regression_schedules",
        ["project_id"],
    )
    op.create_index(
        "ix_regression_schedules_due",
        "regression_schedules",
        ["status", "next_fire_at"],
    )

    op.add_column(
        "test_runs",
        sa.Column("regression_schedule_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "test_runs",
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_test_runs_regression_schedule_id_regression_schedules",
        "test_runs",
        "regression_schedules",
        ["regression_schedule_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_test_runs_regression_schedule_id",
        "test_runs",
        ["regression_schedule_id"],
    )

    op.create_table(
        "regression_schedule_firings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("schedule_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trigger_kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["regression_schedules.id"],
            name="fk_regression_schedule_firings_schedule_id_regression_schedules",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["test_runs.id"],
            name="fk_regression_schedule_firings_run_id_test_runs",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_regression_schedule_firings"),
        sa.UniqueConstraint(
            "schedule_id",
            "scheduled_for",
            name="uq_regression_schedule_firings_schedule_id",
        ),
    )
    op.create_index(
        "ix_regression_schedule_firings_schedule_created",
        "regression_schedule_firings",
        ["schedule_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_regression_schedule_firings_schedule_created",
        table_name="regression_schedule_firings",
    )
    op.drop_table("regression_schedule_firings")
    op.drop_index("ix_test_runs_regression_schedule_id", table_name="test_runs")
    op.drop_constraint(
        "fk_test_runs_regression_schedule_id_regression_schedules",
        "test_runs",
        type_="foreignkey",
    )
    op.drop_column("test_runs", "scheduled_for")
    op.drop_column("test_runs", "regression_schedule_id")
    op.drop_index("ix_regression_schedules_due", table_name="regression_schedules")
    op.drop_index("ix_regression_schedules_project_id", table_name="regression_schedules")
    op.drop_table("regression_schedules")
