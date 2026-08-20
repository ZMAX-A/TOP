"""Add project quality change alert thresholds.

Revision ID: 20260817_0011
Revises: 20260814_0010
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0011"
down_revision: str | None = "20260814_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "quality_alert_warning_drop_points",
            sa.Integer(),
            server_default=sa.text("5"),
            nullable=False,
        ),
    )
    op.add_column(
        "projects",
        sa.Column(
            "quality_alert_critical_drop_points",
            sa.Integer(),
            server_default=sa.text("10"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_projects_quality_alert_warning_drop_points_range"),
        "projects",
        "quality_alert_warning_drop_points BETWEEN 1 AND 100",
    )
    op.create_check_constraint(
        op.f("ck_projects_quality_alert_critical_drop_points_range"),
        "projects",
        "quality_alert_critical_drop_points BETWEEN 1 AND 100",
    )
    op.create_check_constraint(
        op.f("ck_projects_quality_alert_drop_points_order"),
        "projects",
        "quality_alert_warning_drop_points < quality_alert_critical_drop_points",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_projects_quality_alert_drop_points_order"),
        "projects",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_projects_quality_alert_critical_drop_points_range"),
        "projects",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_projects_quality_alert_warning_drop_points_range"),
        "projects",
        type_="check",
    )
    op.drop_column("projects", "quality_alert_critical_drop_points")
    op.drop_column("projects", "quality_alert_warning_drop_points")
