"""User-scoped persistence for AI assistant conversations."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from .persistence import AiChatConversationRecord, AiChatMessageRecord, utc_now
from .schemas import AiChatTurn, AiProjectChatContent


class AiConversationNotFound(RuntimeError):
    """The requested conversation is outside the current user and scope."""


def _conversation_query(
    *,
    conversation_id: UUID,
    user_id: UUID,
    project_id: UUID,
    run_id: UUID | None,
) -> Select[tuple[AiChatConversationRecord]]:
    statement = select(AiChatConversationRecord).where(
        AiChatConversationRecord.id == conversation_id,
        AiChatConversationRecord.user_id == user_id,
        AiChatConversationRecord.project_id == project_id,
    )
    if run_id is None:
        return statement.where(AiChatConversationRecord.run_id.is_(None))
    return statement.where(AiChatConversationRecord.run_id == run_id)


async def list_ai_conversations(
    session: AsyncSession,
    *,
    user_id: UUID,
    project_id: UUID,
    run_id: UUID | None,
    limit: int = 20,
) -> tuple[AiChatConversationRecord, ...]:
    statement = select(AiChatConversationRecord).where(
        AiChatConversationRecord.user_id == user_id,
        AiChatConversationRecord.project_id == project_id,
    )
    if run_id is None:
        statement = statement.where(AiChatConversationRecord.run_id.is_(None))
    else:
        statement = statement.where(AiChatConversationRecord.run_id == run_id)
    records = await session.scalars(
        statement.order_by(AiChatConversationRecord.updated_at.desc()).limit(limit)
    )
    return tuple(records)


async def get_ai_conversation(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    user_id: UUID,
    project_id: UUID,
    run_id: UUID | None,
    for_update: bool = False,
) -> AiChatConversationRecord:
    statement = _conversation_query(
        conversation_id=conversation_id,
        user_id=user_id,
        project_id=project_id,
        run_id=run_id,
    )
    if for_update:
        # The conversation is commonly loaded before the (potentially slow) model
        # request. Another browser request can append messages while that model
        # call is in flight, leaving this session's identity-map copy stale. The
        # row lock serializes writers; populate_existing makes sure the locked
        # row also refreshes message_count before sequence numbers are allocated.
        statement = statement.with_for_update().execution_options(populate_existing=True)
    record = await session.scalar(statement)
    if record is None:
        raise AiConversationNotFound("AI conversation not found in the selected scope")
    return record


async def list_ai_conversation_messages(
    session: AsyncSession,
    conversation_id: UUID,
    *,
    limit: int = 200,
) -> tuple[AiChatMessageRecord, ...]:
    records = await session.scalars(
        select(AiChatMessageRecord)
        .where(AiChatMessageRecord.conversation_id == conversation_id)
        .order_by(AiChatMessageRecord.sequence.asc())
        .limit(limit)
    )
    return tuple(records)


async def load_ai_chat_context(
    session: AsyncSession,
    *,
    conversation_id: UUID | None,
    user_id: UUID,
    project_id: UUID,
    run_id: UUID | None,
    exclude_message_id: UUID | None = None,
) -> tuple[AiChatConversationRecord | None, tuple[AiChatTurn, ...]]:
    if conversation_id is None:
        return None, ()
    conversation = await get_ai_conversation(
        session,
        conversation_id=conversation_id,
        user_id=user_id,
        project_id=project_id,
        run_id=run_id,
    )
    message_query = select(AiChatMessageRecord).where(
        AiChatMessageRecord.conversation_id == conversation.id
    )
    if exclude_message_id is not None:
        message_query = message_query.where(AiChatMessageRecord.id != exclude_message_id)
    messages = await session.scalars(
        message_query
        .order_by(AiChatMessageRecord.sequence.desc())
        .limit(8)
    )
    history = tuple(
        AiChatTurn(role=message.role, content=message.content)
        for message in reversed(tuple(messages))
    )
    return conversation, history


def _conversation_title(question: str) -> str:
    compact = " ".join(question.split())
    return compact if len(compact) <= 60 else f"{compact[:59].rstrip()}…"


async def save_ai_chat_question(
    session: AsyncSession,
    *,
    conversation: AiChatConversationRecord | None,
    user_id: UUID,
    project_id: UUID,
    run_id: UUID | None,
    question: str,
) -> tuple[AiChatConversationRecord, AiChatMessageRecord]:
    """Persist the user turn before a potentially slow model request starts."""

    if conversation is None:
        locked = AiChatConversationRecord(
            id=uuid4(),
            user_id=user_id,
            project_id=project_id,
            run_id=run_id,
            title=_conversation_title(question),
            message_count=0,
        )
        session.add(locked)
    else:
        locked = await get_ai_conversation(
            session,
            conversation_id=conversation.id,
            user_id=user_id,
            project_id=project_id,
            run_id=run_id,
            for_update=True,
        )
    message = AiChatMessageRecord(
        id=uuid4(),
        conversation_id=locked.id,
        sequence=locked.message_count + 1,
        role="user",
        content=question,
        sections=[],
        evidence_refs=[],
        suggested_questions=[],
        response_metadata={"status": "PENDING"},
    )
    session.add(message)
    locked.message_count += 1
    locked.updated_at = utc_now()
    await session.commit()
    await session.refresh(locked)
    await session.refresh(message)
    return locked, message


async def get_ai_chat_question(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    question_message_id: UUID,
) -> AiChatMessageRecord:
    message = await session.scalar(
        select(AiChatMessageRecord).where(
            AiChatMessageRecord.id == question_message_id,
            AiChatMessageRecord.conversation_id == conversation_id,
            AiChatMessageRecord.role == "user",
        )
    )
    if message is None:
        raise AiConversationNotFound("AI question not found in the selected conversation")
    return message


async def save_ai_chat_answer(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    question_message_id: UUID,
    user_id: UUID,
    project_id: UUID,
    run_id: UUID | None,
    content: AiProjectChatContent,
    model: str,
    latency_ms: int,
    generated_at: datetime,
    evidence_truncated: bool,
) -> AiChatConversationRecord:
    locked = await get_ai_conversation(
        session,
        conversation_id=conversation_id,
        user_id=user_id,
        project_id=project_id,
        run_id=run_id,
        for_update=True,
    )
    question = await get_ai_chat_question(
        session,
        conversation_id=locked.id,
        question_message_id=question_message_id,
    )
    existing_answers = await session.scalars(
        select(AiChatMessageRecord).where(
            AiChatMessageRecord.conversation_id == locked.id,
            AiChatMessageRecord.role == "assistant",
        )
    )
    if any(
        record.response_metadata.get("reply_to_message_id") == str(question_message_id)
        for record in existing_answers
    ):
        return locked
    session.add(
        AiChatMessageRecord(
            id=uuid4(),
            conversation_id=locked.id,
            sequence=locked.message_count + 1,
            role="assistant",
            content=content.answer,
            sections=[section.model_dump(mode="json") for section in content.sections],
            evidence_refs=list(content.evidence_refs),
            suggested_questions=list(content.suggested_questions),
            response_metadata={
                "status": "COMPLETED",
                "reply_to_message_id": str(question_message_id),
                "model": model,
                "latency_ms": latency_ms,
                "generated_at": generated_at.isoformat(),
                "evidence_truncated": evidence_truncated,
            },
        )
    )
    question.response_metadata = {**question.response_metadata, "status": "COMPLETED"}
    locked.message_count += 1
    locked.updated_at = utc_now()
    await session.commit()
    await session.refresh(locked)
    return locked


async def mark_ai_chat_question_failed(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    question_message_id: UUID,
) -> None:
    question = await get_ai_chat_question(
        session,
        conversation_id=conversation_id,
        question_message_id=question_message_id,
    )
    question.response_metadata = {**question.response_metadata, "status": "FAILED"}
    await session.commit()


async def save_ai_chat_exchange(
    session: AsyncSession,
    *,
    conversation: AiChatConversationRecord | None,
    user_id: UUID,
    project_id: UUID,
    run_id: UUID | None,
    question: str,
    content: AiProjectChatContent,
    model: str,
    latency_ms: int,
    generated_at: datetime,
    evidence_truncated: bool,
) -> AiChatConversationRecord:
    locked, question_record = await save_ai_chat_question(
        session,
        conversation=conversation,
        user_id=user_id,
        project_id=project_id,
        run_id=run_id,
        question=question,
    )
    return await save_ai_chat_answer(
        session,
        conversation_id=locked.id,
        question_message_id=question_record.id,
        user_id=user_id,
        project_id=project_id,
        run_id=run_id,
        content=content,
        model=model,
        latency_ms=latency_ms,
        generated_at=generated_at,
        evidence_truncated=evidence_truncated,
    )


async def delete_ai_conversation(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    user_id: UUID,
    project_id: UUID,
) -> None:
    record = await session.scalar(
        select(AiChatConversationRecord).where(
            AiChatConversationRecord.id == conversation_id,
            AiChatConversationRecord.user_id == user_id,
            AiChatConversationRecord.project_id == project_id,
        )
    )
    if record is None:
        raise AiConversationNotFound("AI conversation not found in the selected project")
    await session.delete(record)
    await session.commit()
