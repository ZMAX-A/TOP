"""Add automation package supply-chain admission evidence.

Revision ID: 20260820_0017
Revises: 20260820_0016
Create Date: 2026-08-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0017"
down_revision: str | None = "20260820_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


VERIFICATIONS = "automation_package_supply_chain_verifications"


def upgrade() -> None:
    op.create_table(
        VERIFICATIONS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("automation_package_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("verifier", sa.String(length=200), nullable=False),
        sa.Column("image_digest", sa.String(length=71), nullable=False),
        sa.Column("signature_bundle_digest", sa.String(length=71), nullable=False),
        sa.Column("provenance_digest", sa.String(length=71), nullable=False),
        sa.Column("sbom_digest", sa.String(length=71), nullable=False),
        sa.Column("signature_verified", sa.Boolean(), nullable=False),
        sa.Column("transparency_log_verified", sa.Boolean(), nullable=False),
        sa.Column("provenance_verified", sa.Boolean(), nullable=False),
        sa.Column("sbom_verified", sa.Boolean(), nullable=False),
        sa.Column("certificate_issuer", sa.String(length=500), nullable=False),
        sa.Column("certificate_identity", sa.String(length=500), nullable=False),
        sa.Column("builder_id", sa.String(length=500), nullable=False),
        sa.Column("source_repository", sa.String(length=500), nullable=False),
        sa.Column("source_revision", sa.String(length=128), nullable=False),
        sa.Column("report_digest", sa.String(length=71), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("verified_by", sa.Uuid(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('VERIFIED', 'REJECTED')",
            name=op.f("ck_automation_package_supply_chain_verifications_outcome_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["automation_package_id"],
            ["automation_packages.id"],
            name=op.f(
                "fk_automation_package_supply_chain_verifications_automation_package_id_automation_packages"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_automation_package_supply_chain_verifications_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["test_targets.id"],
            name=op.f("fk_automation_package_supply_chain_verifications_target_id_test_targets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["verified_by"],
            ["users.id"],
            name=op.f("fk_automation_package_supply_chain_verifications_verified_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_automation_package_supply_chain_verifications"),
        ),
        sa.UniqueConstraint(
            "automation_package_id",
            "report_digest",
            name=op.f("uq_automation_package_supply_chain_verifications_automation_package_id"),
        ),
    )
    op.create_index(
        op.f("ix_automation_package_supply_chain_verifications_project_id"),
        VERIFICATIONS,
        ["project_id"],
    )
    op.create_index(
        op.f("ix_automation_package_supply_chain_verifications_target_id"),
        VERIFICATIONS,
        ["target_id"],
    )
    op.create_index(
        "ix_package_supply_chain_verifications_package_created",
        VERIFICATIONS,
        ["automation_package_id", "created_at"],
    )
    op.create_index(
        op.f("ix_automation_package_supply_chain_verifications_verified_by"),
        VERIFICATIONS,
        ["verified_by"],
    )

    op.add_column(
        "automation_packages",
        sa.Column(
            "supply_chain_status",
            sa.String(length=32),
            nullable=False,
            server_default="LEGACY",
        ),
    )
    op.add_column(
        "automation_packages",
        sa.Column("supply_chain_verification_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "automation_packages",
        sa.Column("supply_chain_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE automation_packages SET supply_chain_status = 'PENDING' WHERE status = 'DRAFT'"
        )
    )
    op.create_check_constraint(
        op.f("ck_automation_packages_supply_chain_status_allowed"),
        "automation_packages",
        "supply_chain_status IN ('LEGACY', 'PENDING', 'VERIFIED', 'REJECTED')",
    )
    op.create_foreign_key(
        op.f(
            "fk_automation_packages_supply_chain_verification_id_automation_package_supply_chain_verifications"
        ),
        "automation_packages",
        VERIFICATIONS,
        ["supply_chain_verification_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_automation_packages_supply_chain_verification_id"),
        "automation_packages",
        ["supply_chain_verification_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_automation_packages_supply_chain_verification_id"),
        table_name="automation_packages",
    )
    op.drop_constraint(
        op.f(
            "fk_automation_packages_supply_chain_verification_id_automation_package_supply_chain_verifications"
        ),
        "automation_packages",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("ck_automation_packages_supply_chain_status_allowed"),
        "automation_packages",
        type_="check",
    )
    op.drop_column("automation_packages", "supply_chain_verified_at")
    op.drop_column("automation_packages", "supply_chain_verification_id")
    op.drop_column("automation_packages", "supply_chain_status")
    op.drop_table(VERIFICATIONS)
