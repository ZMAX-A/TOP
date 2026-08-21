"""Add asymmetric verifier identity evidence to supply-chain envelopes.

Revision ID: 20260821_0019
Revises: 20260820_0018
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260821_0019"
down_revision: str | None = "20260820_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ENVELOPES = "automation_package_supply_chain_envelopes"
IDENTITY_CONSTRAINT = "ck_supply_chain_envelope_signature_identity"


def upgrade() -> None:
    op.add_column(
        ENVELOPES,
        sa.Column(
            "envelope_profile",
            sa.String(length=64),
            nullable=False,
            server_default="testops-supply-chain-envelope-v1",
        ),
    )
    op.add_column(
        ENVELOPES,
        sa.Column(
            "signature_algorithm",
            sa.String(length=20),
            nullable=False,
            server_default="HMAC-SHA256",
        ),
    )
    op.add_column(
        ENVELOPES,
        sa.Column("workload_identity", sa.String(length=512), nullable=True),
    )
    op.add_column(
        ENVELOPES,
        sa.Column("key_fingerprint", sa.String(length=71), nullable=True),
    )
    op.alter_column(ENVELOPES, "envelope_profile", server_default=None)
    op.alter_column(ENVELOPES, "signature_algorithm", server_default=None)
    op.create_check_constraint(
        IDENTITY_CONSTRAINT,
        ENVELOPES,
        "(signature_algorithm = 'HMAC-SHA256' "
        "AND envelope_profile = 'testops-supply-chain-envelope-v1' "
        "AND workload_identity IS NULL AND key_fingerprint IS NULL) "
        "OR (signature_algorithm = 'ED25519' "
        "AND envelope_profile = 'testops-supply-chain-envelope-v2' "
        "AND workload_identity IS NOT NULL AND key_fingerprint IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(IDENTITY_CONSTRAINT, ENVELOPES, type_="check")
    op.drop_column(ENVELOPES, "key_fingerprint")
    op.drop_column(ENVELOPES, "workload_identity")
    op.drop_column(ENVELOPES, "signature_algorithm")
    op.drop_column(ENVELOPES, "envelope_profile")
