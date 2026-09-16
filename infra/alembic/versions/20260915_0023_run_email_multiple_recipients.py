"""Allow up to five Run notification recipients per user.

Revision ID: 20260915_0023
Revises: 20260914_0022
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260915_0023"
down_revision: str | None = "20260914_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run_email_preferences",
        sa.Column(
            "emails",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
    )
    op.execute(
        sa.text(
            "UPDATE run_email_preferences "
            "SET emails = json_build_array(email) "
            "WHERE email IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.drop_column("run_email_preferences", "emails")
