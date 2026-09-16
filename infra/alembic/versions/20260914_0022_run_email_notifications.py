"""Add user-managed Run result email notifications.

Revision ID: 20260914_0022
Revises: 20260904_0021
Create Date: 2026-09-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260914_0022"
down_revision: str | None = "20260904_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_email_preferences",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("statuses", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "enabled = false OR email IS NOT NULL",
            name="ck_run_email_preferences_enabled_requires_email",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_run_email_preferences_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_run_email_preferences"),
    )
    op.create_table(
        "run_email_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("recipient_email", sa.String(length=320), nullable=False),
        sa.Column("destination_display", sa.String(length=320), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="PENDING", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'DELIVERED', 'FAILED', 'CANCELED')",
            name="ck_run_email_deliveries_status_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_run_email_deliveries_project_id_projects",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["test_runs.id"],
            name="fk_run_email_deliveries_run_id_test_runs",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_run_email_deliveries_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_run_email_deliveries"),
        sa.UniqueConstraint("dedupe_key", name="uq_run_email_deliveries_dedupe_key"),
    )
    op.create_index(
        "ix_run_email_deliveries_pending",
        "run_email_deliveries",
        ["status", "available_at"],
    )
    op.create_index(
        "ix_run_email_deliveries_user_created",
        "run_email_deliveries",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_run_email_deliveries_user_id",
        "run_email_deliveries",
        ["user_id"],
    )
    op.create_index(
        "ix_run_email_deliveries_project_id",
        "run_email_deliveries",
        ["project_id"],
    )
    op.create_index(
        "ix_run_email_deliveries_run_id",
        "run_email_deliveries",
        ["run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_run_email_deliveries_run_id", table_name="run_email_deliveries")
    op.drop_index("ix_run_email_deliveries_project_id", table_name="run_email_deliveries")
    op.drop_index("ix_run_email_deliveries_user_id", table_name="run_email_deliveries")
    op.drop_index("ix_run_email_deliveries_user_created", table_name="run_email_deliveries")
    op.drop_index("ix_run_email_deliveries_pending", table_name="run_email_deliveries")
    op.drop_table("run_email_deliveries")
    op.drop_table("run_email_preferences")
