from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import uuid4

from testops.api.ai_chat_services import (
    delete_ai_conversation,
    list_ai_conversation_messages,
    list_ai_conversations,
    load_ai_chat_context,
    save_ai_chat_answer,
    save_ai_chat_exchange,
    save_ai_chat_question,
)
from testops.api.config import Settings
from testops.api.database import create_database_runtime, create_schema
from testops.api.persistence import ProjectRecord, UserRecord
from testops.api.schemas import AiProjectChatContent


class AiChatPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine, self.session_factory = create_database_runtime(
            Settings(database_url="sqlite+aiosqlite:///:memory:")
        )
        await create_schema(self.engine)
        self.user_id = uuid4()
        self.project_id = uuid4()
        async with self.session_factory() as session:
            session.add_all(
                (
                    UserRecord(
                        id=self.user_id,
                        username="ai-history-user",
                        display_name="AI History User",
                        password_hash="unit-test-hash",
                        system_role="USER",
                        status="ACTIVE",
                    ),
                    ProjectRecord(
                        id=self.project_id,
                        key="ai-history",
                        name="AI History",
                        description=None,
                    ),
                )
            )
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_exchange_survives_new_session_and_can_be_deleted(self) -> None:
        content = AiProjectChatContent(
            answer="最近运行存在基础设施异常。",
            sections=({"title": "事实依据", "items": ("Run 未进入业务测试。",)},),
            evidence_refs=("run:test",),
            suggested_questions=("需要检查哪些日志？",),
        )
        generated_at = datetime.now(UTC)
        async with self.session_factory() as session:
            conversation = await save_ai_chat_exchange(
                session,
                conversation=None,
                user_id=self.user_id,
                project_id=self.project_id,
                run_id=None,
                question="总结最近运行",
                content=content,
                model="unit-test-model",
                latency_ms=12,
                generated_at=generated_at,
                evidence_truncated=False,
            )
            conversation_id = conversation.id

        async with self.session_factory() as session:
            conversations = await list_ai_conversations(
                session,
                user_id=self.user_id,
                project_id=self.project_id,
                run_id=None,
            )
            restored, history = await load_ai_chat_context(
                session,
                conversation_id=conversation_id,
                user_id=self.user_id,
                project_id=self.project_id,
                run_id=None,
            )
            messages = await list_ai_conversation_messages(session, conversation_id)

            self.assertEqual(len(conversations), 1)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.message_count, 2)
            self.assertEqual([turn.role for turn in history], ["user", "assistant"])
            self.assertEqual(messages[1].sections[0]["title"], "事实依据")
            self.assertEqual(messages[1].response_metadata["model"], "unit-test-model")

            await delete_ai_conversation(
                session,
                conversation_id=conversation_id,
                user_id=self.user_id,
                project_id=self.project_id,
            )

        async with self.session_factory() as session:
            conversations = await list_ai_conversations(
                session,
                user_id=self.user_id,
                project_id=self.project_id,
                run_id=None,
            )
            self.assertEqual(conversations, ())

    async def test_question_is_visible_in_a_new_session_before_answer_finishes(self) -> None:
        content = AiProjectChatContent(
            answer="持久化后的回答",
            sections=({"title": "事实依据", "items": ("问题已提前保存。",)},),
        )
        generated_at = datetime.now(UTC)
        async with self.session_factory() as session:
            conversation, question = await save_ai_chat_question(
                session,
                conversation=None,
                user_id=self.user_id,
                project_id=self.project_id,
                run_id=None,
                question="刷新页面后还能看到吗？",
            )
            conversation_id = conversation.id
            question_id = question.id

        async with self.session_factory() as refreshed_session:
            restored, history = await load_ai_chat_context(
                refreshed_session,
                conversation_id=conversation_id,
                user_id=self.user_id,
                project_id=self.project_id,
                run_id=None,
            )
            messages = await list_ai_conversation_messages(
                refreshed_session,
                conversation_id,
            )

            self.assertIsNotNone(restored)
            self.assertEqual(restored.message_count, 1)
            self.assertEqual([turn.content for turn in history], ["刷新页面后还能看到吗？"])
            self.assertEqual(messages[0].response_metadata["status"], "PENDING")

            await save_ai_chat_answer(
                refreshed_session,
                conversation_id=conversation_id,
                question_message_id=question_id,
                user_id=self.user_id,
                project_id=self.project_id,
                run_id=None,
                content=content,
                model="unit-test-model",
                latency_ms=2,
                generated_at=generated_at,
                evidence_truncated=False,
            )

        async with self.session_factory() as session:
            messages = await list_ai_conversation_messages(session, conversation_id)
            self.assertEqual([message.role for message in messages], ["user", "assistant"])
            self.assertEqual(messages[0].response_metadata["status"], "COMPLETED")
            self.assertEqual(
                messages[1].response_metadata["reply_to_message_id"],
                str(question_id),
            )

    async def test_stale_conversation_snapshot_is_refreshed_before_append(self) -> None:
        content = AiProjectChatContent(
            answer="回答",
            sections=({"title": "事实依据", "items": ("测试证据",)},),
            evidence_refs=(),
            suggested_questions=(),
        )
        generated_at = datetime.now(UTC)
        async with self.session_factory() as session:
            conversation = await save_ai_chat_exchange(
                session,
                conversation=None,
                user_id=self.user_id,
                project_id=self.project_id,
                run_id=None,
                question="第一问",
                content=content,
                model="unit-test-model",
                latency_ms=1,
                generated_at=generated_at,
                evidence_truncated=False,
            )
            conversation_id = conversation.id

        async with self.session_factory() as stale_session:
            stale_conversation, _ = await load_ai_chat_context(
                stale_session,
                conversation_id=conversation_id,
                user_id=self.user_id,
                project_id=self.project_id,
                run_id=None,
            )
            self.assertIsNotNone(stale_conversation)
            self.assertEqual(stale_conversation.message_count, 2)

            async with self.session_factory() as concurrent_session:
                current_conversation, _ = await load_ai_chat_context(
                    concurrent_session,
                    conversation_id=conversation_id,
                    user_id=self.user_id,
                    project_id=self.project_id,
                    run_id=None,
                )
                await save_ai_chat_exchange(
                    concurrent_session,
                    conversation=current_conversation,
                    user_id=self.user_id,
                    project_id=self.project_id,
                    run_id=None,
                    question="第二问",
                    content=content,
                    model="unit-test-model",
                    latency_ms=1,
                    generated_at=generated_at,
                    evidence_truncated=False,
                )

            await save_ai_chat_exchange(
                stale_session,
                conversation=stale_conversation,
                user_id=self.user_id,
                project_id=self.project_id,
                run_id=None,
                question="第三问",
                content=content,
                model="unit-test-model",
                latency_ms=1,
                generated_at=generated_at,
                evidence_truncated=False,
            )

        async with self.session_factory() as session:
            conversation, _ = await load_ai_chat_context(
                session,
                conversation_id=conversation_id,
                user_id=self.user_id,
                project_id=self.project_id,
                run_id=None,
            )
            messages = await list_ai_conversation_messages(session, conversation_id)

            self.assertIsNotNone(conversation)
            self.assertEqual(conversation.message_count, 6)
            self.assertEqual([message.sequence for message in messages], list(range(1, 7)))
