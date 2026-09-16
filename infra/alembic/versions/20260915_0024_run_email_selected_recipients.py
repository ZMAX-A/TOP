"""Select active recipients from configured Run notification emails.

Revision ID: 20260915_0024
Revises: 20260915_0023
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260915_0024"
down_revision: str | None = "20260915_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run_email_preferences",
        sa.Column(
            "selected_emails",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
    )
    op.execute(
        sa.text(
            "UPDATE run_email_preferences "
            "SET selected_emails = emails"
        )
    )


def downgrade() -> None:
    op.drop_column("run_email_preferences", "selected_emails")
