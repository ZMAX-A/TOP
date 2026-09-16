from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from testops.api.ai_analysis import (
    AiAnalysisError,
    AiEvidencePack,
    build_project_chat_evidence,
    build_run_evidence,
    redact_text,
    sanitize_and_validate_analysis,
    sanitize_and_validate_chat,
)
from testops.api.platform_knowledge import build_platform_knowledge_evidence
from testops.api.schemas import AiProbableCause, AiProjectChatContent, AiRunAnalysisContent


class AiAnalysisTests(unittest.TestCase):
    def test_redact_text_removes_credentials_and_personal_data(self) -> None:
        source = (
            "Authorization: Bearer abcdefghijklmnop "
            "api_key=sk-example123456 password=hunter2 "
            "owner@example.com 13800138000"
        )
        redacted = redact_text(source)
        for sensitive in (
            "abcdefghijklmnop",
            "sk-example123456",
            "hunter2",
            "owner@example.com",
            "13800138000",
        ):
            self.assertNotIn(sensitive, redacted)

    def test_evidence_is_bounded_redacted_and_excludes_artifact_uri(self) -> None:
        moment = datetime.now(UTC)
        run_id = uuid4()
        run = SimpleNamespace(
            id=run_id,
            status="INFRA_ERROR",
            case_count=1,
            timeout_seconds=3600,
            dispatch_state="DISPATCHED",
            dispatch_wait_reason=None,
            cancel_requested=False,
            created_at=moment,
            started_at=moment,
            finished_at=moment,
            error_message="token=super-secret-token owner@example.com",
            snapshot={
                "case_baseline": {"version": "case-v1", "digest": "sha256:baseline"},
                "automation_package": {
                    "name": "web",
                    "version": "1.0.1",
                    "digest": "sha256:package",
                },
            },
            result_document={
                "execution_isolation": {
                    "mode": "CONTAINER",
                    "memory_limit_bytes": 1024,
                    "credential": "must-not-pass",
                }
            },
        )
        cases = [
            SimpleNamespace(
                case_code="TC-001",
                sequence=1,
                status="INFRA_ERROR",
                duration_ms=123,
                failure_category="INFRASTRUCTURE",
                error_message="password=secret-value " + "x" * 1200,
            )
        ]
        events = [
            SimpleNamespace(
                sequence=index,
                source="RUNNER",
                event_type="progress_updated",
                case_code="TC-001",
                status="RUNNING",
                occurred_at=moment,
                payload={
                    "completed": index,
                    "error_message": "Bearer abcdefghijklmnop " + "y" * 500,
                    "untrusted_secret": "must-not-pass",
                },
            )
            for index in range(1, 51)
        ]
        artifact_id = uuid4()
        artifacts = [
            SimpleNamespace(
                artifact_id=artifact_id,
                kind="LOG",
                name="failure.log",
                uri="s3://private-bucket/path?token=must-not-pass",
                digest="sha256:artifact",
                size_bytes=42,
                created_at=moment,
            )
        ]
        evidence = build_run_evidence(
            run,
            cases,
            events,
            artifacts,
            max_chars=4_000,
        )
        self.assertLessEqual(len(evidence.serialized), 4_000)
        self.assertTrue(evidence.truncated)
        for sensitive in (
            "super-secret-token",
            "owner@example.com",
            "secret-value",
            "abcdefghijklmnop",
            "must-not-pass",
            "s3://private-bucket",
        ):
            self.assertNotIn(sensitive, evidence.serialized)
        self.assertIn(f"run:{run_id}", evidence.evidence_refs)
        self.assertIn("case:TC-001", evidence.evidence_refs)
        self.assertIn(f"artifact:{artifact_id}", evidence.evidence_refs)

    def test_analysis_rejects_an_invented_evidence_reference(self) -> None:
        analysis = AiRunAnalysisContent(
            summary="脚本可能失败",
            classification="AUTOMATION_SCRIPT",
            confidence=0.7,
            probable_causes=(
                AiProbableCause(
                    title="元素定位失败",
                    detail="页面没有匹配元素",
                    category="AUTOMATION_SCRIPT",
                    confidence=0.7,
                    evidence_refs=("case:INVENTED",),
                ),
            ),
            recommendations=("检查定位器",),
        )
        with self.assertRaisesRegex(AiAnalysisError, "not supplied"):
            sanitize_and_validate_analysis(analysis, frozenset({"case:TC-001"}))

    def test_analysis_output_is_redacted_again(self) -> None:
        analysis = AiRunAnalysisContent(
            summary="联系 owner@example.com，token=secret-value",
            classification="UNKNOWN",
            confidence=0.2,
        )
        sanitized = sanitize_and_validate_analysis(analysis, frozenset())
        self.assertNotIn("owner@example.com", sanitized.summary)
        self.assertNotIn("secret-value", sanitized.summary)

    def test_project_chat_evidence_combines_recent_stats_and_selected_run(self) -> None:
        project_id = uuid4()
        run_id = uuid4()
        moment = datetime.now(UTC)
        run = SimpleNamespace(
            id=run_id,
            status="FAILED",
            case_count=3,
            created_at=moment,
            started_at=moment,
            finished_at=moment,
            snapshot={
                "case_baseline": {"version": "case-v1.0.1"},
                "automation_package": {"version": "1.0.1"},
            },
        )
        selected = AiEvidencePack(
            document={},
            serialized=(
                '{"run":{"evidence_ref":"run:selected"},"abnormal_cases":[],'
                '"recent_events":[],"artifact_metadata":[]}'
            ),
            evidence_refs=frozenset({"run:selected"}),
            analyzed_case_count=0,
            truncated=False,
        )
        evidence = build_project_chat_evidence(
            project_id,
            [run],
            selected_run=selected,
            knowledge_query="Runner Pool 为什么排队",
            max_chars=5_000,
        )
        self.assertFalse(evidence.truncated)
        self.assertIn(f"project:{project_id}", evidence.evidence_refs)
        self.assertIn(f"run:{run_id}", evidence.evidence_refs)
        self.assertIn("run:selected", evidence.evidence_refs)
        self.assertIn('"schema_version":"testops-ai-project-chat-evidence-v3"', evidence.serialized)
        self.assertIn('"counting_unit":"RUN_RECORD"', evidence.serialized)
        self.assertIn('"requested_run_count":10', evidence.serialized)
        self.assertIn('"FAILED":1', evidence.serialized)
        self.assertNotIn("all_time_case_status_counts", evidence.serialized)
        self.assertNotIn("case_count", evidence.serialized)
        self.assertIn("knowledge:runner-pool", evidence.evidence_refs)
        self.assertIn("NO_HEALTHY_RUNNER", evidence.serialized)

    def test_platform_knowledge_selects_runner_pool_and_exposes_navigation(self) -> None:
        document, refs = build_platform_knowledge_evidence("Runner Pool 为什么排队，在哪里配置？")

        self.assertIn("knowledge:runner-pool", refs)
        self.assertEqual(document["scope"], "VERSIONED_TOP_PRODUCT_KNOWLEDGE")
        matched = document["matched_entries"]
        self.assertEqual(matched[0]["evidence_ref"], "knowledge:runner-pool")
        self.assertIn("系统管理 > Runner Pool", matched[0]["navigation"])
        self.assertIn("NO_HEALTHY_RUNNER", " ".join(matched[0]["facts"]))

    def test_chat_rejects_invented_reference_and_redacts_answer(self) -> None:
        invented = AiProjectChatContent(
            answer="结论",
            sections=({"title": "事实依据", "items": ("一条事实。",)},),
            evidence_refs=("run:invented",),
        )
        with self.assertRaisesRegex(AiAnalysisError, "not supplied"):
            sanitize_and_validate_chat(invented, frozenset({"run:real"}))
        safe = sanitize_and_validate_chat(
            AiProjectChatContent(
                answer="**owner@example.com token=secret-value。**",
                sections=(
                    {
                        "title": "**事实依据**",
                        "items": ("**owner@example.com token=secret-value。**",),
                    },
                ),
                evidence_refs=("run:real",),
                suggested_questions=("**继续检查**",),
            ),
            frozenset({"run:real"}),
        )
        self.assertNotIn("owner@example.com", safe.answer)
        self.assertNotIn("secret-value", safe.answer)
        self.assertNotIn("**", safe.answer)
        self.assertEqual(safe.sections[0].title, "事实依据")
        self.assertNotIn("owner@example.com", safe.sections[0].items[0])
        self.assertNotIn("secret-value", safe.sections[0].items[0])
        self.assertNotIn("**", safe.sections[0].items[0])
        self.assertEqual(safe.suggested_questions, ("继续检查",))

    def test_chat_rejects_obviously_incomplete_answer(self) -> None:
        with self.assertRaisesRegex(AiAnalysisError, "incomplete"):
            sanitize_and_validate_chat(
                AiProjectChatContent(
                    answer="证据显示最近运行共有十次，而",
                    sections=({"title": "事实依据", "items": ("一条事实。",)},),
                ),
                frozenset(),
            )

    def test_chat_bounds_structured_output_after_model_validation(self) -> None:
        refs = tuple(f"run:{index}" for index in range(20))
        content = AiProjectChatContent(
            answer="结论" * 300,
            sections=tuple(
                {
                    "title": f"第 {section_index} 个分析部分" * 8,
                    "items": tuple("详情" * 600 for _ in range(8)),
                }
                for section_index in range(7)
            ),
            evidence_refs=refs,
            suggested_questions=tuple(f"继续问题 {index}" for index in range(8)),
        )

        safe = sanitize_and_validate_chat(content, frozenset(refs))

        self.assertLessEqual(len(safe.answer), 500)
        self.assertEqual(len(safe.sections), 5)
        self.assertTrue(all(len(section.title) <= 48 for section in safe.sections))
        self.assertTrue(all(len(section.items) == 6 for section in safe.sections))
        self.assertTrue(
            all(len(item) <= 1_000 for section in safe.sections for item in section.items)
        )
        self.assertEqual(len(safe.evidence_refs), 12)
        self.assertEqual(len(safe.suggested_questions), 4)
