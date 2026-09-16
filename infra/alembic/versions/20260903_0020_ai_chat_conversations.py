"""Persist user-scoped AI assistant conversations.

Revision ID: 20260903_0020
Revises: 20260821_0019
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260903_0020"
down_revision: str | None = "20260821_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_DOCUMENT = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()),
    "postgresql",
)


def upgrade() -> None:
    op.create_table(
        "ai_chat_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("message_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "message_count >= 0",
            name="ck_ai_chat_conversations_message_count_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_ai_chat_conversations_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_ai_chat_conversations_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["test_runs.id"],
            name="fk_ai_chat_conversations_run_id_test_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_chat_conversations"),
    )
    op.create_index(
        "ix_ai_chat_conversations_user_id",
        "ai_chat_conversations",
        ["user_id"],
    )
    op.create_index(
        "ix_ai_chat_conversations_project_id",
        "ai_chat_conversations",
        ["project_id"],
    )
    op.create_index(
        "ix_ai_chat_conversations_run_id",
        "ai_chat_conversations",
        ["run_id"],
    )
    op.create_index(
        "ix_ai_chat_conversations_user_project_scope_updated",
        "ai_chat_conversations",
        ["user_id", "project_id", "run_id", "updated_at"],
    )

    op.create_table(
        "ai_chat_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sections", JSON_DOCUMENT, nullable=False),
        sa.Column("evidence_refs", JSON_DOCUMENT, nullable=False),
        sa.Column("suggested_questions", JSON_DOCUMENT, nullable=False),
        sa.Column("response_metadata", JSON_DOCUMENT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_ai_chat_messages_role_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["ai_chat_conversations.id"],
            name="fk_ai_chat_messages_conversation_id_ai_chat_conversations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_chat_messages"),
        sa.UniqueConstraint(
            "conversation_id",
            "sequence",
            name="uq_ai_chat_messages_conversation_id",
        ),
    )
    op.create_index(
        "ix_ai_chat_messages_conversation_id",
        "ai_chat_messages",
        ["conversation_id"],
    )
    op.create_index(
        "ix_ai_chat_messages_conversation_created",
        "ai_chat_messages",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_chat_messages_conversation_created",
        table_name="ai_chat_messages",
    )
    op.drop_index("ix_ai_chat_messages_conversation_id", table_name="ai_chat_messages")
    op.drop_table("ai_chat_messages")
    op.drop_index(
        "ix_ai_chat_conversations_user_project_scope_updated",
        table_name="ai_chat_conversations",
    )
    op.drop_index("ix_ai_chat_conversations_run_id", table_name="ai_chat_conversations")
    op.drop_index("ix_ai_chat_conversations_project_id", table_name="ai_chat_conversations")
    op.drop_index("ix_ai_chat_conversations_user_id", table_name="ai_chat_conversations")
    op.drop_table("ai_chat_conversations")
