"""Add project execution admission quotas.

Revision ID: 20260813_0005
Revises: 20260812_0004
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_0005"
down_revision: str | None = "20260812_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "max_in_flight_runs",
            sa.Integer(),
            server_default=sa.text("20"),
            nullable=False,
        ),
    )
    op.add_column(
        "projects",
        sa.Column(
            "max_daily_runs",
            sa.Integer(),
            server_default=sa.text("500"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_projects_max_in_flight_runs_range"),
        "projects",
        "max_in_flight_runs BETWEEN 1 AND 500",
    )
    op.create_check_constraint(
        op.f("ck_projects_max_daily_runs_range"),
        "projects",
        "max_daily_runs BETWEEN 1 AND 100000",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_projects_max_daily_runs_range"),
        "projects",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_projects_max_in_flight_runs_range"),
        "projects",
        type_="check",
    )
    op.drop_column("projects", "max_daily_runs")
    op.drop_column("projects", "max_in_flight_runs")
