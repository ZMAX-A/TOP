"""Add durable quality Webhook configuration and delivery records.

Revision ID: 20260817_0012
Revises: 20260817_0011
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260817_0012"
down_revision: str | None = "20260817_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quality_webhook_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("endpoint_url", sa.Text(), nullable=False),
        sa.Column(
            "minimum_alert_status",
            sa.String(length=16),
            server_default=sa.text("'WARNING'"),
            nullable=False,
        ),
        sa.Column("signing_secret_name", sa.String(length=100), nullable=True),
        sa.Column("signing_secret_ref", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "minimum_alert_status IN ('WARNING', 'CRITICAL')",
            name=op.f("ck_quality_webhook_configs_minimum_alert_status_values"),
        ),
        sa.CheckConstraint(
            "(signing_secret_name IS NULL AND signing_secret_ref IS NULL) "
            "OR (signing_secret_name IS NOT NULL AND signing_secret_ref IS NOT NULL)",
            name=op.f("ck_quality_webhook_configs_signing_secret_pair"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_quality_webhook_configs_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quality_webhook_configs")),
        sa.UniqueConstraint(
            "project_id",
            name=op.f("uq_quality_webhook_configs_project_id"),
        ),
    )
    op.create_index(
        op.f("ix_quality_webhook_configs_project_id"),
        "quality_webhook_configs",
        ["project_id"],
        unique=False,
    )
    op.create_table(
        "quality_webhook_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("webhook_config_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("destination_display", sa.String(length=500), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_quality_webhook_deliveries_attempts_non_negative"),
        ),
        sa.CheckConstraint(
            "response_status IS NULL OR response_status BETWEEN 100 AND 599",
            name=op.f("ck_quality_webhook_deliveries_response_status_range"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'DELIVERED', 'FAILED')",
            name=op.f("ck_quality_webhook_deliveries_status_values"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_quality_webhook_deliveries_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["webhook_config_id"],
            ["quality_webhook_configs.id"],
            name=op.f("fk_quality_webhook_deliveries_webhook_config_id_quality_webhook_configs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quality_webhook_deliveries")),
        sa.UniqueConstraint(
            "dedupe_key",
            name=op.f("uq_quality_webhook_deliveries_dedupe_key"),
        ),
    )
    op.create_index(
        "ix_quality_webhook_deliveries_project_created",
        "quality_webhook_deliveries",
        ["project_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_quality_webhook_deliveries_status_available",
        "quality_webhook_deliveries",
        ["status", "available_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_quality_webhook_deliveries_status_available",
        table_name="quality_webhook_deliveries",
    )
    op.drop_index(
        "ix_quality_webhook_deliveries_project_created",
        table_name="quality_webhook_deliveries",
    )
    op.drop_table("quality_webhook_deliveries")
    op.drop_index(
        op.f("ix_quality_webhook_configs_project_id"),
        table_name="quality_webhook_configs",
    )
    op.drop_table("quality_webhook_configs")
