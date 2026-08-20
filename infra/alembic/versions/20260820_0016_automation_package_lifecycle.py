"""Add governed automation package lifecycle metadata.

Revision ID: 20260820_0016
Revises: 20260817_0015
Create Date: 2026-08-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0016"
down_revision: str | None = "20260817_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "automation_packages",
        sa.Column(
            "runner_type",
            sa.String(length=32),
            nullable=False,
            server_default="WEB_PLAYWRIGHT",
        ),
    )
    op.add_column(
        "automation_packages",
        sa.Column(
            "image_repository",
            sa.String(length=500),
            nullable=False,
            server_default="testops-worker",
        ),
    )
    op.add_column(
        "automation_packages",
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "automation_packages",
        sa.Column("validated_run_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "automation_packages",
        sa.Column("activated_by", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "automation_packages",
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "automation_packages",
        sa.Column("status_reason", sa.String(length=500), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_automation_packages_runner_type_allowed"),
        "automation_packages",
        "runner_type IN ('WEB_PLAYWRIGHT')",
    )
    op.create_check_constraint(
        op.f("ck_automation_packages_status_allowed"),
        "automation_packages",
        "status IN ('DRAFT', 'ACTIVE', 'DEPRECATED', 'REVOKED')",
    )
    op.create_foreign_key(
        op.f("fk_automation_packages_supersedes_id_automation_packages"),
        "automation_packages",
        "automation_packages",
        ["supersedes_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_automation_packages_validated_run_id_test_runs"),
        "automation_packages",
        "test_runs",
        ["validated_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_automation_packages_activated_by_users"),
        "automation_packages",
        "users",
        ["activated_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_automation_packages_supersedes_id"),
        "automation_packages",
        ["supersedes_id"],
    )
    op.create_index(
        op.f("ix_automation_packages_validated_run_id"),
        "automation_packages",
        ["validated_run_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_automation_packages_validated_run_id"),
        table_name="automation_packages",
    )
    op.drop_index(
        op.f("ix_automation_packages_supersedes_id"),
        table_name="automation_packages",
    )
    op.drop_constraint(
        op.f("fk_automation_packages_activated_by_users"),
        "automation_packages",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_automation_packages_validated_run_id_test_runs"),
        "automation_packages",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_automation_packages_supersedes_id_automation_packages"),
        "automation_packages",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("ck_automation_packages_status_allowed"),
        "automation_packages",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_automation_packages_runner_type_allowed"),
        "automation_packages",
        type_="check",
    )
    op.drop_column("automation_packages", "status_reason")
    op.drop_column("automation_packages", "activated_at")
    op.drop_column("automation_packages", "activated_by")
    op.drop_column("automation_packages", "validated_run_id")
    op.drop_column("automation_packages", "supersedes_id")
    op.drop_column("automation_packages", "image_repository")
    op.drop_column("automation_packages", "runner_type")
