"""Add Run retry lineage for operational reruns.

Revision ID: 20260812_0004
Revises: 20260812_0003
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_0004"
down_revision: str | None = "20260812_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("test_runs", sa.Column("source_run_id", sa.Uuid(), nullable=True))
    op.add_column("test_runs", sa.Column("retry_mode", sa.String(length=32), nullable=True))
    op.create_foreign_key(
        "fk_test_runs_source_run_id_test_runs",
        "test_runs",
        "test_runs",
        ["source_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_test_runs_source_run_id", "test_runs", ["source_run_id"])


def downgrade() -> None:
    op.drop_index("ix_test_runs_source_run_id", table_name="test_runs")
    op.drop_constraint(
        "fk_test_runs_source_run_id_test_runs",
        "test_runs",
        type_="foreignkey",
    )
    op.drop_column("test_runs", "retry_mode")
    op.drop_column("test_runs", "source_run_id")
