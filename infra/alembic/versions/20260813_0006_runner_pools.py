"""Add Runner Pools, worker heartbeats and pool slot leases.

Revision ID: 20260813_0006
Revises: 20260813_0005
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260813_0006"
down_revision: str | None = "20260813_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_DOCUMENT = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "runner_pools",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("target_types", JSON_DOCUMENT, nullable=False),
        sa.Column("queue_name", sa.String(length=128), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "max_concurrency BETWEEN 1 AND 500",
            name="ck_runner_pools_max_concurrency_range",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_runner_pools"),
        sa.UniqueConstraint("key", name="uq_runner_pools_key"),
        sa.UniqueConstraint("queue_name", name="uq_runner_pools_queue_name"),
    )
    op.create_table(
        "runner_workers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pool_id", sa.Uuid(), nullable=False),
        sa.Column("worker_key", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("runner_version", sa.String(length=64), nullable=False),
        sa.Column("capabilities", JSON_DOCUMENT, nullable=False),
        sa.Column("max_slots", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "max_slots BETWEEN 1 AND 100",
            name="ck_runner_workers_max_slots_range",
        ),
        sa.ForeignKeyConstraint(
            ["pool_id"],
            ["runner_pools.id"],
            name="fk_runner_workers_pool_id_runner_pools",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_runner_workers"),
        sa.UniqueConstraint("worker_key", name="uq_runner_workers_worker_key"),
    )
    op.create_index("ix_runner_workers_pool_id", "runner_workers", ["pool_id"])
    op.create_index(
        "ix_runner_workers_last_heartbeat_at",
        "runner_workers",
        ["last_heartbeat_at"],
    )

    for table_name in ("test_targets", "environments", "test_runs"):
        op.add_column(table_name, sa.Column("runner_pool_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            f"fk_{table_name}_runner_pool_id_runner_pools",
            table_name,
            "runner_pools",
            ["runner_pool_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(
            f"ix_{table_name}_runner_pool_id",
            table_name,
            ["runner_pool_id"],
        )

    op.add_column(
        "test_runs",
        sa.Column(
            "dispatch_state",
            sa.String(length=32),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
    )
    op.add_column(
        "test_runs",
        sa.Column("dispatch_wait_reason", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "test_runs",
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE test_runs SET dispatch_state = 'DISPATCHED', dispatched_at = created_at "
        "WHERE status IN ('PASSED', 'FAILED', 'CANCELED', 'TIMED_OUT', 'INFRA_ERROR')"
    )

    op.create_table(
        "runner_slot_leases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pool_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["pool_id"],
            ["runner_pools.id"],
            name="fk_runner_slot_leases_pool_id_runner_pools",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["test_runs.id"],
            name="fk_runner_slot_leases_run_id_test_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_runner_slot_leases"),
    )
    op.create_index(
        "ix_runner_slot_leases_pool_status",
        "runner_slot_leases",
        ["pool_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_runner_slot_leases_pool_status", table_name="runner_slot_leases")
    op.drop_table("runner_slot_leases")
    op.drop_column("test_runs", "dispatched_at")
    op.drop_column("test_runs", "dispatch_wait_reason")
    op.drop_column("test_runs", "dispatch_state")
    for table_name in ("test_runs", "environments", "test_targets"):
        op.drop_index(f"ix_{table_name}_runner_pool_id", table_name=table_name)
        op.drop_constraint(
            f"fk_{table_name}_runner_pool_id_runner_pools",
            table_name,
            type_="foreignkey",
        )
        op.drop_column(table_name, "runner_pool_id")
    op.drop_index("ix_runner_workers_last_heartbeat_at", table_name="runner_workers")
    op.drop_index("ix_runner_workers_pool_id", table_name="runner_workers")
    op.drop_table("runner_workers")
    op.drop_table("runner_pools")
