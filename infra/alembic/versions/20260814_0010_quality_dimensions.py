"""Add quality dimension query indexes.

Revision ID: 20260814_0010
Revises: 20260813_0009
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260814_0010"
down_revision: str | None = "20260813_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


QUALITY_DIMENSION_INDEXES = (
    ("ix_test_runs_project_target_status_finished", "target_id"),
    ("ix_test_runs_project_environment_status_finished", "environment_id"),
    ("ix_test_runs_project_baseline_status_finished", "baseline_id"),
)


def upgrade() -> None:
    for index_name, dimension_column in QUALITY_DIMENSION_INDEXES:
        op.create_index(
            index_name,
            "test_runs",
            ["project_id", dimension_column, "status", "finished_at"],
        )


def downgrade() -> None:
    for index_name, _dimension_column in reversed(QUALITY_DIMENSION_INDEXES):
        op.drop_index(index_name, table_name="test_runs")
