"""Add Run timeouts and worker-bound slot lease recovery.

Revision ID: 20260813_0008
Revises: 20260813_0007
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_0008"
down_revision: str | None = "20260813_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "run_timeout_seconds",
            sa.Integer(),
            server_default=sa.text("3600"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_projects_run_timeout_seconds_range"),
        "projects",
        "run_timeout_seconds BETWEEN 60 AND 86400",
    )

    op.add_column(
        "test_runs",
        sa.Column(
            "timeout_seconds",
            sa.Integer(),
            server_default=sa.text("3600"),
            nullable=False,
        ),
    )
    op.add_column(
        "test_runs",
        sa.Column("timeout_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_test_runs_timeout_seconds_range"),
        "test_runs",
        "timeout_seconds BETWEEN 60 AND 86400",
    )
    op.create_index("ix_test_runs_timeout_at", "test_runs", ["timeout_at"])
    op.execute(
        "UPDATE test_runs AS runs SET timeout_seconds = projects.run_timeout_seconds "
        "FROM projects WHERE projects.id = runs.project_id"
    )
    op.execute(
        "UPDATE test_runs SET timeout_at = "
        "COALESCE(started_at, dispatched_at, updated_at) + make_interval(secs => timeout_seconds) "
        "WHERE status IN ('PREPARING', 'RUNNING')"
    )

    op.add_column(
        "runner_slot_leases",
        sa.Column("worker_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_runner_slot_leases_worker_id_runner_workers",
        "runner_slot_leases",
        "runner_workers",
        ["worker_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_runner_slot_leases_worker_id",
        "runner_slot_leases",
        ["worker_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_runner_slot_leases_worker_id",
        table_name="runner_slot_leases",
    )
    op.drop_constraint(
        "fk_runner_slot_leases_worker_id_runner_workers",
        "runner_slot_leases",
        type_="foreignkey",
    )
    op.drop_column("runner_slot_leases", "worker_id")
    op.drop_index("ix_test_runs_timeout_at", table_name="test_runs")
    op.drop_constraint(
        op.f("ck_test_runs_timeout_seconds_range"),
        "test_runs",
        type_="check",
    )
    op.drop_column("test_runs", "timeout_at")
    op.drop_column("test_runs", "timeout_seconds")
    op.drop_constraint(
        op.f("ck_projects_run_timeout_seconds_range"),
        "projects",
        type_="check",
    )
    op.drop_column("projects", "run_timeout_seconds")
