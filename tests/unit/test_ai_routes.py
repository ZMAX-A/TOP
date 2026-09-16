from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch
from uuid import uuid4

from fastapi import HTTPException

from testops.api.ai_analysis import AiEvidencePack
from testops.api.ai_gateway import AiGatewayError, AiStructuredResult
from testops.api.ai_routes import (
    post_project_ai_chat,
    post_project_ai_chat_question,
    post_run_failure_analysis,
)
from testops.api.schemas import (
    AiChatQuestionRequest,
    AiChatTurn,
    AiProbableCause,
    AiProjectChatContent,
    AiProjectChatRequest,
    AiRunAnalysisContent,
)


class AiRunAnalysisRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_chat_question_is_persisted_before_model_request(self) -> None:
        project_id = uuid4()
        user_id = uuid4()
        conversation_id = uuid4()
        message_id = uuid4()
        created_at = datetime.now(UTC)
        conversation = SimpleNamespace(id=conversation_id)
        message = SimpleNamespace(id=message_id, created_at=created_at)
        save_question = AsyncMock(return_value=(conversation, message))
        session = SimpleNamespace()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        schedule_completion = Mock()

        with (
            patch("testops.api.ai_routes.authorize_project", AsyncMock()),
            patch("testops.api.ai_routes.save_ai_chat_question", save_question),
            patch(
                "testops.api.ai_routes._schedule_project_ai_chat_completion",
                schedule_completion,
            ),
        ):
            response = await post_project_ai_chat_question(
                project_id,
                AiChatQuestionRequest(
                    message="刷新后保留这个问题",
                    model_provider_key="environment",
                ),
                request,
                SimpleNamespace(user_id=user_id),
                session,
            )

        save_question.assert_awaited_once_with(
            session,
            conversation=None,
            user_id=user_id,
            project_id=project_id,
            run_id=None,
            question="刷新后保留这个问题",
        )
        schedule_completion.assert_called_once_with(
            request=request,
            principal=ANY,
            project_id=project_id,
            run_id=None,
            conversation_id=conversation_id,
            question_message_id=message_id,
            model_provider_key="environment",
        )
        self.assertEqual(response.conversation_id, conversation_id)
        self.assertEqual(response.message_id, message_id)

    async def test_analysis_reuses_run_read_authorization_and_returns_metadata(self) -> None:
        run_id = uuid4()
        project_id = uuid4()
        run = SimpleNamespace(id=run_id, project_id=project_id, status="FAILED")
        evidence = AiEvidencePack(
            document={},
            serialized='{"run":{"evidence_ref":"run:test"}}',
            evidence_refs=frozenset({"run:test"}),
            analyzed_case_count=1,
            truncated=False,
        )
        content = AiRunAnalysisContent(
            summary="断言失败",
            classification="ASSERTION",
            confidence=0.75,
            probable_causes=(
                AiProbableCause(
                    title="预期文本不可见",
                    detail="运行证据记录了断言失败",
                    category="ASSERTION",
                    confidence=0.75,
                    evidence_refs=("run:test",),
                ),
            ),
            recommendations=("检查断言与页面状态",),
        )
        gateway = SimpleNamespace(
            complete_json=AsyncMock(
                return_value=AiStructuredResult(
                    content=content,
                    model="unit-test-model",
                    latency_ms=12,
                )
            )
        )
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    ai_gateway=gateway,
                    settings=SimpleNamespace(ai_max_evidence_chars=24_000),
                )
            )
        )
        authorize = AsyncMock()
        with (
            patch("testops.api.ai_routes.get_run", AsyncMock(return_value=run)),
            patch("testops.api.ai_routes.authorize_project", authorize),
            patch("testops.api.ai_routes.list_run_cases", AsyncMock(return_value=())),
            patch("testops.api.ai_routes.list_run_events", AsyncMock(return_value=())),
            patch("testops.api.ai_routes.list_run_artifacts", AsyncMock(return_value=())),
            patch("testops.api.ai_routes.build_run_evidence", return_value=evidence),
        ):
            response = await post_run_failure_analysis(
                run_id,
                request,
                SimpleNamespace(user_id=uuid4()),
                SimpleNamespace(),
            )
        authorize.assert_awaited_once_with(
            ANY,
            ANY,
            project_id,
            "run:read",
        )
        self.assertEqual(response.run_id, run_id)
        self.assertEqual(response.classification, "ASSERTION")
        self.assertEqual(response.model, "unit-test-model")
        self.assertEqual(response.analyzed_case_count, 1)

    async def test_analysis_rejects_non_terminal_run_before_model_call(self) -> None:
        run_id = uuid4()
        run = SimpleNamespace(id=run_id, project_id=uuid4(), status="RUNNING")
        with (
            patch("testops.api.ai_routes.get_run", AsyncMock(return_value=run)),
            patch("testops.api.ai_routes.authorize_project", AsyncMock()),
        ):
            with self.assertRaises(HTTPException) as raised:
                await post_run_failure_analysis(
                    run_id,
                    SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
                    SimpleNamespace(user_id=uuid4()),
                    SimpleNamespace(),
                )
        self.assertEqual(raised.exception.status_code, 409)

    async def test_project_chat_returns_read_only_model_answer(self) -> None:
        project_id = uuid4()
        conversation_id = uuid4()
        evidence = AiEvidencePack(
            document={},
            serialized='{"project":{"evidence_ref":"project:test"}}',
            evidence_refs=frozenset({"project:test"}),
            analyzed_case_count=0,
            truncated=False,
        )
        incomplete_content = AiProjectChatContent(
            answer="证据显示最近运行共有十次，而",
            sections=({"title": "事实依据", "items": ("共获取十次运行。",)},),
            evidence_refs=("project:test",),
        )
        content = AiProjectChatContent(
            answer="**最近 10 次运行情况稳定。**",
            sections=(
                {"title": "**事实依据**", "items": ("最近 10 次 Run 均已完成。",)},
                {"title": "建议步骤", "items": ("继续观察下一次全量测试。",)},
            ),
            evidence_refs=("project:test",),
            suggested_questions=("是否存在失败 Run？",),
        )
        gateway = SimpleNamespace(
            complete_json=AsyncMock(
                side_effect=(
                    AiStructuredResult(
                        content=incomplete_content,
                        model="unit-test-model",
                        latency_ms=8,
                        finish_reason="stop",
                    ),
                    AiStructuredResult(
                        content=content,
                        model="unit-test-model",
                        latency_ms=10,
                        finish_reason="stop",
                    ),
                )
            )
        )
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    ai_gateway=gateway,
                    settings=SimpleNamespace(ai_max_evidence_chars=24_000),
                )
            )
        )
        authorize = AsyncMock()
        list_runs_mock = AsyncMock(return_value=((), 0))
        build_evidence_mock = Mock(return_value=evidence)
        resolve_gateway = AsyncMock(return_value=gateway)
        session = SimpleNamespace()
        with (
            patch("testops.api.ai_routes.authorize_project", authorize),
            patch("testops.api.ai_routes._resolve_gateway", resolve_gateway),
            patch("testops.api.ai_routes.list_runs", list_runs_mock),
            patch(
                "testops.api.ai_routes.build_project_chat_evidence",
                build_evidence_mock,
            ),
            patch(
                "testops.api.ai_routes.save_ai_chat_exchange",
                AsyncMock(return_value=SimpleNamespace(id=conversation_id)),
            ),
        ):
            response = await post_project_ai_chat(
                project_id,
                AiProjectChatRequest(
                    message="在哪里配置？",
                    model_provider_key="environment",
                    history=(AiChatTurn(role="user", content="Runner Pool 是什么？"),),
                ),
                request,
                SimpleNamespace(user_id=uuid4()),
                session,
            )
        authorize.assert_awaited_once_with(ANY, ANY, project_id, "run:read")
        resolve_gateway.assert_awaited_once_with(request, session, "environment")
        list_runs_mock.assert_awaited_once_with(session, project_id, limit=10, offset=0)
        self.assertEqual(
            build_evidence_mock.call_args.kwargs["knowledge_query"],
            "Runner Pool 是什么？\n在哪里配置？",
        )
        self.assertEqual(gateway.complete_json.await_count, 2)
        self.assertEqual(response.project_id, project_id)
        self.assertEqual(response.conversation_id, conversation_id)
        self.assertEqual(response.answer, "最近 10 次运行情况稳定。")
        self.assertEqual(response.sections[0].title, "事实依据")
        self.assertEqual(response.sections[1].items, ("继续观察下一次全量测试。",))
        self.assertEqual(response.model, "unit-test-model")
        self.assertEqual(response.latency_ms, 18)

    async def test_prepared_question_appends_only_the_answer(self) -> None:
        project_id = uuid4()
        user_id = uuid4()
        conversation_id = uuid4()
        question_id = uuid4()
        conversation = SimpleNamespace(id=conversation_id)
        prepared_question = SimpleNamespace(id=question_id, content="已持久化的问题")
        evidence = AiEvidencePack(
            document={},
            serialized='{"project":{"evidence_ref":"project:test"}}',
            evidence_refs=frozenset({"project:test"}),
            analyzed_case_count=0,
            truncated=False,
        )
        content = AiProjectChatContent(
            answer="问题刷新后仍然存在。",
            sections=({"title": "事实依据", "items": ("问题已提前持久化。",)},),
            evidence_refs=("project:test",),
        )
        gateway = SimpleNamespace(
            complete_json=AsyncMock(
                return_value=AiStructuredResult(
                    content=content,
                    model="unit-test-model",
                    latency_ms=7,
                    finish_reason="stop",
                )
            )
        )
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    ai_gateway=gateway,
                    settings=SimpleNamespace(ai_max_evidence_chars=24_000),
                )
            )
        )
        session = SimpleNamespace()
        save_answer = AsyncMock(return_value=conversation)
        save_exchange = AsyncMock()
        build_evidence = Mock(return_value=evidence)
        load_context = AsyncMock(
            return_value=(
                conversation,
                (AiChatTurn(role="assistant", content="上一条回答"),),
            )
        )

        with (
            patch("testops.api.ai_routes.authorize_project", AsyncMock()),
            patch("testops.api.ai_routes._resolve_gateway", AsyncMock(return_value=gateway)),
            patch("testops.api.ai_routes.list_runs", AsyncMock(return_value=((), 0))),
            patch("testops.api.ai_routes.load_ai_chat_context", load_context),
            patch(
                "testops.api.ai_routes.get_ai_chat_question",
                AsyncMock(return_value=prepared_question),
            ),
            patch("testops.api.ai_routes.build_project_chat_evidence", build_evidence),
            patch("testops.api.ai_routes.save_ai_chat_answer", save_answer),
            patch("testops.api.ai_routes.save_ai_chat_exchange", save_exchange),
        ):
            response = await post_project_ai_chat(
                project_id,
                AiProjectChatRequest(
                    message="客户端副本",
                    conversation_id=conversation_id,
                    question_message_id=question_id,
                ),
                request,
                SimpleNamespace(user_id=user_id),
                session,
            )

        self.assertEqual(
            load_context.await_args.kwargs["exclude_message_id"],
            question_id,
        )
        self.assertEqual(
            build_evidence.call_args.kwargs["knowledge_query"],
            "上一条回答\n已持久化的问题",
        )
        save_answer.assert_awaited_once()
        save_exchange.assert_not_awaited()
        self.assertEqual(response.conversation_id, conversation_id)
        self.assertEqual(response.answer, "问题刷新后仍然存在。")

    async def test_project_chat_retries_invalid_structured_model_output(self) -> None:
        project_id = uuid4()
        conversation_id = uuid4()
        evidence = AiEvidencePack(
            document={},
            serialized='{"project":{"evidence_ref":"project:test"}}',
            evidence_refs=frozenset({"project:test"}),
            analyzed_case_count=0,
            truncated=False,
        )
        content = AiProjectChatContent(
            answer="最近一次运行存在基础设施异常。",
            sections=({"title": "事实依据", "items": ("Run 状态为异常。",)},),
            evidence_refs=("project:test",),
        )
        gateway = SimpleNamespace(
            complete_json=AsyncMock(
                side_effect=(
                    AiGatewayError("AI model returned an invalid structured analysis"),
                    AiStructuredResult(
                        content=content,
                        model="unit-test-model",
                        latency_ms=11,
                        finish_reason="stop",
                    ),
                )
            )
        )
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    ai_gateway=gateway,
                    settings=SimpleNamespace(ai_max_evidence_chars=24_000),
                )
            )
        )
        list_runs_mock = AsyncMock(return_value=((), 0))
        with (
            patch("testops.api.ai_routes.authorize_project", AsyncMock()),
            patch("testops.api.ai_routes.list_runs", list_runs_mock),
            patch("testops.api.ai_routes.build_project_chat_evidence", return_value=evidence),
            patch(
                "testops.api.ai_routes.save_ai_chat_exchange",
                AsyncMock(return_value=SimpleNamespace(id=conversation_id)),
            ),
        ):
            response = await post_project_ai_chat(
                project_id,
                AiProjectChatRequest(message="总结近期测试"),
                request,
                SimpleNamespace(user_id=uuid4()),
                SimpleNamespace(),
            )

        self.assertEqual(gateway.complete_json.await_count, 2)
        retry_prompt = gateway.complete_json.await_args_list[1].kwargs["system_prompt"]
        self.assertIn("did not match the required JSON Schema", retry_prompt)
        self.assertEqual(response.answer, "最近一次运行存在基础设施异常。")

    async def test_project_chat_retries_invented_evidence_reference(self) -> None:
        project_id = uuid4()
        conversation_id = uuid4()
        evidence = AiEvidencePack(
            document={},
            serialized='{"project":{"evidence_ref":"project:test"}}',
            evidence_refs=frozenset({"project:test"}),
            analyzed_case_count=0,
            truncated=False,
        )
        invented = AiProjectChatContent(
            answer="我是 TOP AI 助手。",
            sections=({"title": "能力说明", "items": ("提供只读分析。",)},),
            evidence_refs=("knowledge:invented",),
        )
        corrected = invented.model_copy(update={"evidence_refs": ()})
        gateway = SimpleNamespace(
            complete_json=AsyncMock(
                side_effect=(
                    AiStructuredResult(
                        content=invented,
                        model="unit-test-model",
                        latency_ms=7,
                        finish_reason="stop",
                    ),
                    AiStructuredResult(
                        content=corrected,
                        model="unit-test-model",
                        latency_ms=8,
                        finish_reason="stop",
                    ),
                )
            )
        )
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    ai_gateway=gateway,
                    settings=SimpleNamespace(ai_max_evidence_chars=24_000),
                )
            )
        )
        with (
            patch("testops.api.ai_routes.authorize_project", AsyncMock()),
            patch("testops.api.ai_routes.list_runs", AsyncMock(return_value=((), 0))),
            patch("testops.api.ai_routes.build_project_chat_evidence", return_value=evidence),
            patch(
                "testops.api.ai_routes.save_ai_chat_exchange",
                AsyncMock(return_value=SimpleNamespace(id=conversation_id)),
            ),
        ):
            response = await post_project_ai_chat(
                project_id,
                AiProjectChatRequest(message="你是谁"),
                request,
                SimpleNamespace(user_id=uuid4()),
                SimpleNamespace(),
            )

        self.assertEqual(gateway.complete_json.await_count, 2)
        retry_prompt = gateway.complete_json.await_args_list[1].kwargs["system_prompt"]
        self.assertIn("Use only evidence_ref values", retry_prompt)
        self.assertEqual(response.answer, "我是 TOP AI 助手。")
        self.assertEqual(response.evidence_refs, ())

    async def test_project_chat_retries_transient_model_connection_failure(self) -> None:
        project_id = uuid4()
        conversation_id = uuid4()
        evidence = AiEvidencePack(
            document={},
            serialized='{"project":{"evidence_ref":"project:test"}}',
            evidence_refs=frozenset({"project:test"}),
            analyzed_case_count=0,
            truncated=False,
        )
        content = AiProjectChatContent(
            answer="模型连接已恢复。",
            sections=({"title": "状态", "items": ("回答已生成。",)},),
            evidence_refs=(),
        )
        gateway = SimpleNamespace(
            complete_json=AsyncMock(
                side_effect=(
                    AiGatewayError("AI model endpoint is unreachable"),
                    AiStructuredResult(
                        content=content,
                        model="unit-test-model",
                        latency_ms=9,
                        finish_reason="stop",
                    ),
                )
            )
        )
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    ai_gateway=gateway,
                    settings=SimpleNamespace(ai_max_evidence_chars=24_000),
                )
            )
        )
        sleep = AsyncMock()
        with (
            patch("testops.api.ai_routes.authorize_project", AsyncMock()),
            patch("testops.api.ai_routes.asyncio.sleep", sleep),
            patch("testops.api.ai_routes.list_runs", AsyncMock(return_value=((), 0))),
            patch("testops.api.ai_routes.build_project_chat_evidence", return_value=evidence),
            patch(
                "testops.api.ai_routes.save_ai_chat_exchange",
                AsyncMock(return_value=SimpleNamespace(id=conversation_id)),
            ),
        ):
            response = await post_project_ai_chat(
                project_id,
                AiProjectChatRequest(message="检查连接"),
                request,
                SimpleNamespace(user_id=uuid4()),
                SimpleNamespace(),
            )

        sleep.assert_awaited_once_with(1)
        self.assertEqual(gateway.complete_json.await_count, 2)
        self.assertEqual(response.answer, "模型连接已恢复。")
