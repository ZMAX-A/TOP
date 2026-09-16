"""Allow immutable Android Appium automation packages.

Revision ID: 20260916_0025
Revises: 20260915_0024
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260916_0025"
down_revision: str | None = "20260915_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_automation_packages_runner_type_allowed"),
        "automation_packages",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_automation_packages_runner_type_allowed"),
        "automation_packages",
        "runner_type IN ('WEB_PLAYWRIGHT', 'ANDROID_APPIUM')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_automation_packages_runner_type_allowed"),
        "automation_packages",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_automation_packages_runner_type_allowed"),
        "automation_packages",
        "runner_type IN ('WEB_PLAYWRIGHT')",
    )
