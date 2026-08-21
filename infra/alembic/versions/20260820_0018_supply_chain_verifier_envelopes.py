"""Add signed supply-chain verifier envelope replay records.

Revision ID: 20260820_0018
Revises: 20260820_0017
Create Date: 2026-08-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0018"
down_revision: str | None = "20260820_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ENVELOPES = "automation_package_supply_chain_envelopes"


def upgrade() -> None:
    op.create_table(
        ENVELOPES,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("automation_package_id", sa.Uuid(), nullable=False),
        sa.Column("verification_id", sa.Uuid(), nullable=False),
        sa.Column("verifier", sa.String(length=200), nullable=False),
        sa.Column("credential_id", sa.String(length=129), nullable=False),
        sa.Column("nonce", sa.String(length=36), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_digest", sa.String(length=71), nullable=False),
        sa.Column("signature_digest", sa.String(length=71), nullable=False),
        sa.ForeignKeyConstraint(
            ["automation_package_id"],
            ["automation_packages.id"],
            name=op.f(
                "fk_automation_package_supply_chain_envelopes_automation_package_id_automation_packages"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_automation_package_supply_chain_envelopes_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["test_targets.id"],
            name=op.f("fk_automation_package_supply_chain_envelopes_target_id_test_targets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["verification_id"],
            ["automation_package_supply_chain_verifications.id"],
            name=op.f(
                "fk_automation_package_supply_chain_envelopes_verification_id_automation_package_supply_chain_verifications"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_automation_package_supply_chain_envelopes"),
        ),
        sa.UniqueConstraint(
            "credential_id",
            "nonce",
            name=op.f("uq_automation_package_supply_chain_envelopes_credential_id"),
        ),
    )
    op.create_index(
        op.f("ix_automation_package_supply_chain_envelopes_project_id"),
        ENVELOPES,
        ["project_id"],
    )
    op.create_index(
        op.f("ix_automation_package_supply_chain_envelopes_target_id"),
        ENVELOPES,
        ["target_id"],
    )
    op.create_index(
        op.f("ix_automation_package_supply_chain_envelopes_verification_id"),
        ENVELOPES,
        ["verification_id"],
    )
    op.create_index(
        "ix_package_supply_chain_envelopes_package_received",
        ENVELOPES,
        ["automation_package_id", "received_at"],
    )


def downgrade() -> None:
    op.drop_table(ENVELOPES)
