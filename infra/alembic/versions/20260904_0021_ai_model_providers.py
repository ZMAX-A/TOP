"""Add encrypted, administrator-managed AI model providers.

Revision ID: 20260904_0021
Revises: 20260903_0020
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_0021"
down_revision: str | None = "20260903_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_model_providers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "provider_type",
            sa.String(length=32),
            server_default="OPENAI_COMPATIBLE",
            nullable=False,
        ),
        sa.Column("base_url", sa.String(length=2000), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("api_key_ciphertext", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="ACTIVE", nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider_type = 'OPENAI_COMPATIBLE'",
            name="ck_ai_model_providers_provider_type_supported",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED')",
            name="ck_ai_model_providers_status_allowed",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_model_providers"),
        sa.UniqueConstraint("name", name="uq_ai_model_providers_name"),
    )
    op.create_index(
        "ix_ai_model_providers_default_status",
        "ai_model_providers",
        ["is_default", "status"],
    )
    op.create_index(
        "uq_ai_model_providers_single_default",
        "ai_model_providers",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
        sqlite_where=sa.text("is_default = 1"),
    )


def downgrade() -> None:
    op.drop_index("uq_ai_model_providers_single_default", table_name="ai_model_providers")
    op.drop_index("ix_ai_model_providers_default_status", table_name="ai_model_providers")
    op.drop_table("ai_model_providers")
