"""Add scheduled quality alert evaluation state.

Revision ID: 20260817_0013
Revises: 20260817_0012
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0013"
down_revision: str | None = "20260817_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "quality_webhook_configs",
        sa.Column(
            "cooldown_seconds",
            sa.Integer(),
            server_default=sa.text("3600"),
            nullable=False,
        ),
    )
    op.add_column(
        "quality_webhook_configs",
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "quality_webhook_configs",
        sa.Column("next_evaluation_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_quality_webhook_configs_cooldown_seconds_range"),
        "quality_webhook_configs",
        "cooldown_seconds BETWEEN 60 AND 86400",
    )
    op.create_index(
        "ix_quality_webhook_configs_enabled_due",
        "quality_webhook_configs",
        ["enabled", "next_evaluation_at"],
        unique=False,
    )
    op.create_table(
        "quality_alert_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("current_status", sa.String(length=16), nullable=False),
        sa.Column("active_notification_status", sa.String(length=16), nullable=True),
        sa.Column("current_percent", sa.Float(), nullable=True),
        sa.Column("previous_percent", sa.Float(), nullable=True),
        sa.Column("delta_percentage_points", sa.Float(), nullable=True),
        sa.Column("signal_fingerprint", sa.String(length=71), nullable=False),
        sa.Column("notification_sequence", sa.Integer(), nullable=False),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_transition_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_delivery_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "active_notification_status IS NULL "
            "OR active_notification_status IN ('WARNING', 'CRITICAL')",
            name=op.f("ck_quality_alert_states_active_notification_status_values"),
        ),
        sa.CheckConstraint(
            "current_percent IS NULL OR current_percent BETWEEN 0 AND 100",
            name=op.f("ck_quality_alert_states_current_percent_range"),
        ),
        sa.CheckConstraint(
            "current_status IN ('NO_DATA', 'STABLE', 'WARNING', 'CRITICAL')",
            name=op.f("ck_quality_alert_states_current_status_values"),
        ),
        sa.CheckConstraint(
            "delta_percentage_points IS NULL OR delta_percentage_points BETWEEN -100 AND 100",
            name=op.f("ck_quality_alert_states_delta_percentage_points_range"),
        ),
        sa.CheckConstraint(
            "metric IN ('RUN_PASS_RATE', 'CASE_PASS_RATE', 'EXECUTION_RELIABILITY')",
            name=op.f("ck_quality_alert_states_metric_values"),
        ),
        sa.CheckConstraint(
            "notification_sequence >= 0",
            name=op.f("ck_quality_alert_states_notification_sequence_non_negative"),
        ),
        sa.CheckConstraint(
            "previous_percent IS NULL OR previous_percent BETWEEN 0 AND 100",
            name=op.f("ck_quality_alert_states_previous_percent_range"),
        ),
        sa.ForeignKeyConstraint(
            ["last_delivery_id"],
            ["quality_webhook_deliveries.id"],
            name=op.f("fk_quality_alert_states_last_delivery_id_quality_webhook_deliveries"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_quality_alert_states_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quality_alert_states")),
        sa.UniqueConstraint(
            "project_id",
            "metric",
            name=op.f("uq_quality_alert_states_project_id"),
        ),
    )
    op.create_index(
        "ix_quality_alert_states_project_status",
        "quality_alert_states",
        ["project_id", "current_status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_quality_alert_states_project_status",
        table_name="quality_alert_states",
    )
    op.drop_table("quality_alert_states")
    op.drop_index(
        "ix_quality_webhook_configs_enabled_due",
        table_name="quality_webhook_configs",
    )
    op.drop_constraint(
        op.f("ck_quality_webhook_configs_cooldown_seconds_range"),
        "quality_webhook_configs",
        type_="check",
    )
    op.drop_column("quality_webhook_configs", "next_evaluation_at")
    op.drop_column("quality_webhook_configs", "last_evaluated_at")
    op.drop_column("quality_webhook_configs", "cooldown_seconds")
