"""Add durable quality Webhook delivery replay lineage.

Revision ID: 20260817_0015
Revises: 20260817_0014
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0015"
down_revision: str | None = "20260817_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "quality_webhook_deliveries",
        sa.Column("replay_of_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "quality_webhook_deliveries",
        sa.Column("replayed_by", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "quality_webhook_deliveries",
        sa.Column("replay_reason", sa.String(length=500), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_quality_webhook_deliveries_replay_fields_consistent"),
        "quality_webhook_deliveries",
        "(replay_of_id IS NULL AND replayed_by IS NULL AND replay_reason IS NULL) "
        "OR (replay_of_id IS NOT NULL AND replayed_by IS NOT NULL "
        "AND replay_reason IS NOT NULL)",
    )
    op.create_foreign_key(
        op.f("fk_quality_webhook_deliveries_replay_of_id_quality_webhook_deliveries"),
        "quality_webhook_deliveries",
        "quality_webhook_deliveries",
        ["replay_of_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        op.f("uq_quality_webhook_deliveries_replay_of_id"),
        "quality_webhook_deliveries",
        ["replay_of_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("uq_quality_webhook_deliveries_replay_of_id"),
        "quality_webhook_deliveries",
        type_="unique",
    )
    op.drop_constraint(
        op.f("fk_quality_webhook_deliveries_replay_of_id_quality_webhook_deliveries"),
        "quality_webhook_deliveries",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("ck_quality_webhook_deliveries_replay_fields_consistent"),
        "quality_webhook_deliveries",
        type_="check",
    )
    op.drop_column("quality_webhook_deliveries", "replay_reason")
    op.drop_column("quality_webhook_deliveries", "replayed_by")
    op.drop_column("quality_webhook_deliveries", "replay_of_id")
