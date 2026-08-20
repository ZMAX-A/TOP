"""Add quality alert acknowledgement and silence operations.

Revision ID: 20260817_0014
Revises: 20260817_0013
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0014"
down_revision: str | None = "20260817_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "quality_webhook_configs",
        sa.Column("silenced_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "quality_webhook_configs",
        sa.Column("silenced_by", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "quality_webhook_configs",
        sa.Column("silence_reason", sa.String(length=500), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_quality_webhook_configs_silence_fields_consistent"),
        "quality_webhook_configs",
        "(silenced_until IS NULL AND silenced_by IS NULL AND silence_reason IS NULL) "
        "OR (silenced_until IS NOT NULL AND silenced_by IS NOT NULL "
        "AND silence_reason IS NOT NULL)",
    )

    op.add_column(
        "quality_alert_states",
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "quality_alert_states",
        sa.Column("acknowledged_by", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "quality_alert_states",
        sa.Column("acknowledgement_note", sa.String(length=500), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_quality_alert_states_acknowledgement_fields_consistent"),
        "quality_alert_states",
        "(acknowledged_at IS NULL AND acknowledged_by IS NULL "
        "AND acknowledgement_note IS NULL) "
        "OR (acknowledged_at IS NOT NULL AND acknowledged_by IS NOT NULL "
        "AND acknowledgement_note IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_quality_alert_states_acknowledgement_fields_consistent"),
        "quality_alert_states",
        type_="check",
    )
    op.drop_column("quality_alert_states", "acknowledgement_note")
    op.drop_column("quality_alert_states", "acknowledged_by")
    op.drop_column("quality_alert_states", "acknowledged_at")

    op.drop_constraint(
        op.f("ck_quality_webhook_configs_silence_fields_consistent"),
        "quality_webhook_configs",
        type_="check",
    )
    op.drop_column("quality_webhook_configs", "silence_reason")
    op.drop_column("quality_webhook_configs", "silenced_by")
    op.drop_column("quality_webhook_configs", "silenced_until")
