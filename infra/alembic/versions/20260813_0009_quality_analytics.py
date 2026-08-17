"""Add project quality SLO policy.

Revision ID: 20260813_0009
Revises: 20260813_0008
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_0009"
down_revision: str | None = "20260813_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "quality_slo_target_percent",
            sa.Integer(),
            server_default=sa.text("95"),
            nullable=False,
        ),
    )
    op.add_column(
        "projects",
        sa.Column(
            "quality_slo_window_days",
            sa.Integer(),
            server_default=sa.text("30"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_projects_quality_slo_target_percent_range"),
        "projects",
        "quality_slo_target_percent BETWEEN 1 AND 100",
    )
    op.create_check_constraint(
        op.f("ck_projects_quality_slo_window_days_range"),
        "projects",
        "quality_slo_window_days BETWEEN 7 AND 90",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_projects_quality_slo_window_days_range"),
        "projects",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_projects_quality_slo_target_percent_range"),
        "projects",
        type_="check",
    )
    op.drop_column("projects", "quality_slo_window_days")
    op.drop_column("projects", "quality_slo_target_percent")
