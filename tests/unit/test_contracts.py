from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError

from testops.contracts import (
    AssertionDefinition,
    AutomationPackageRef,
    CaseBaseline,
    CaseBaselineRef,
    CaseBaselineSource,
    CaseDefinition,
    CasePriority,
    CaseResult,
    CaseResultStatus,
    RunExecutionIsolationEvidence,
    RunResult,
    RunSnapshot,
    RunStatus,
    RuntimeVariable,
    SecretBinding,
    StepDefinition,
    TargetType,
    WebRunConfig,
)

DIGEST = "sha256:" + "a" * 64


def case_definition() -> CaseDefinition:
    return CaseDefinition(
        case_id=UUID("10000000-0000-0000-0000-000000000001"),
        case_code="TC-LOGIN-001",
        module_key="login",
        module_name="账号登录",
        title="登录成功",
        test_point="验证正常登录流程",
        priority=CasePriority.P0,
        preconditions=("打开登录页面",),
        steps=(
            StepDefinition(operation="input", locator="#username", input="${TEST_USERNAME}"),
            StepDefinition(operation="click", locator="button[type=submit]"),
        ),
        assertion=AssertionDefinition(type="url_contains", expected="/"),
        tags=("smoke",),
    )


def run_snapshot() -> RunSnapshot:
    return RunSnapshot(
        run_id=UUID("20000000-0000-0000-0000-000000000001"),
        project_id=UUID("30000000-0000-0000-0000-000000000001"),
        target_id=UUID("40000000-0000-0000-0000-000000000001"),
        target_type=TargetType.WEB,
        environment_id=UUID("50000000-0000-0000-0000-000000000001"),
        case_baseline=CaseBaselineRef(
            baseline_id=UUID("70000000-0000-0000-0000-000000000001"),
            version="case-v1.0.0",
            digest=DIGEST,
            case_count=1,
        ),
        automation_package=AutomationPackageRef(
            name="yanjia-web",
            version="1.0.0",
            digest=DIGEST,
        ),
        cases=(case_definition(),),
        browser="chromium",
        web_config=WebRunConfig(base_url="https://example.invalid/login"),
        config_hash=DIGEST,
        variables=(RuntimeVariable(name="STORE_NAME", value=""),),
        secret_bindings=(
            SecretBinding(
                name="TEST_USERNAME",
                ref="secret://yanjia/staging/test-username",
            ),
            SecretBinding(
                name="TEST_PASSWORD",
                ref="secret://yanjia/staging/test-password",
            ),
        ),
        created_by=UUID("60000000-0000-0000-0000-000000000001"),
        created_at=datetime(2026, 8, 11, 15, 0, tzinfo=UTC),
    )


class ContractTests(unittest.TestCase):
    def test_alias_is_canonicalized_inside_models(self) -> None:
        step = StepDefinition(
            operation="date_range",
            locator=".date-input",
            input="2026-08-01|2026-08-11",
        )
        assertion = AssertionDefinition(type="visible_text", expected="完成")
        self.assertEqual(step.operation, "daterange")
        self.assertEqual(assertion.type, "text_visible")

    def test_required_locator_is_enforced(self) -> None:
        with self.assertRaisesRegex(ValidationError, "requires locator"):
            StepDefinition(operation="click")

    def test_whitespace_only_input_is_preserved(self) -> None:
        step = StepDefinition(operation="input", locator="#tag", input=" ")
        self.assertEqual(step.input, " ")

    def test_nav_can_take_its_url_from_locator(self) -> None:
        step = StepDefinition(operation="nav", locator="/customer")
        self.assertIsNone(step.input)

    def test_case_baseline_rejects_duplicate_cases(self) -> None:
        case = case_definition()
        with self.assertRaisesRegex(ValidationError, "duplicate case_id"):
            CaseBaseline(
                baseline_id=UUID("70000000-0000-0000-0000-000000000001"),
                project_key="yanjia-ai-web",
                version="case-v1.0.0",
                source=CaseBaselineSource(
                    file_name="test_case.xlsx",
                    file_digest=DIGEST,
                    worksheet="自动化测试用例",
                ),
                cases=(case, case),
            )

    def test_web_snapshot_requires_browser(self) -> None:
        payload = run_snapshot().model_dump()
        payload["browser"] = None
        with self.assertRaisesRegex(ValidationError, "WEB run requires browser"):
            RunSnapshot.model_validate(payload)

    def test_snapshot_is_frozen_and_secret_values_are_absent(self) -> None:
        snapshot = run_snapshot()
        self.assertEqual(
            {binding.name for binding in snapshot.secret_bindings},
            {"TEST_USERNAME", "TEST_PASSWORD"},
        )
        dumped = snapshot.model_dump(mode="json")
        self.assertNotIn("a-real-secret-value", str(dumped))
        with self.assertRaises(ValidationError):
            snapshot.browser = "firefox"
        with self.assertRaises(ValidationError):
            snapshot.cases[0].title = "mutated"
        with self.assertRaises(ValidationError):
            snapshot.cases[0].steps[0].locator = "#changed"

    def test_snapshot_rejects_raw_secret_values(self) -> None:
        payload = run_snapshot().model_dump()
        payload["secret_bindings"][0]["ref"] = "plain-text-password"
        with self.assertRaisesRegex(ValidationError, "string_pattern_mismatch"):
            RunSnapshot.model_validate(payload)

    def test_snapshot_rejects_more_cases_than_baseline(self) -> None:
        payload = run_snapshot().model_dump()
        second_case = case_definition().model_copy(
            update={
                "case_id": UUID("10000000-0000-0000-0000-000000000002"),
                "case_code": "TC-LOGIN-002",
            }
        )
        payload["cases"] = (*payload["cases"], second_case.model_dump())
        with self.assertRaisesRegex(ValidationError, "case count exceeds"):
            RunSnapshot.model_validate(payload)

    def test_secret_like_runtime_variable_requires_secret_binding(self) -> None:
        with self.assertRaisesRegex(ValidationError, "must use a secret binding"):
            RuntimeVariable(name="TEST_PASSWORD", value="not-allowed")

    def test_run_result_isolation_evidence_is_explicit_and_backward_compatible(self) -> None:
        snapshot = run_snapshot()
        moment = datetime.now(UTC)
        result = RunResult(
            run_id=snapshot.run_id,
            status=RunStatus.PASSED,
            started_at=moment,
            finished_at=moment,
            runner_version="0.27.0",
            case_results=(
                CaseResult(
                    case_id=snapshot.cases[0].case_id,
                    case_code=snapshot.cases[0].case_code,
                    status=CaseResultStatus.PASSED,
                    started_at=moment,
                    finished_at=moment,
                    duration_ms=0,
                ),
            ),
        )
        self.assertIsNone(result.execution_isolation)

        evidence = RunExecutionIsolationEvidence(
            mode="SUBPROCESS",
            executor_version="0.27.0",
            dedicated_process=True,
            credential_scope="RUN_SECRETS_ONLY",
            read_only_root_filesystem=False,
            network_policy="WORKER_DEFAULT",
            resource_limits_enforced=False,
        )
        enriched = result.model_copy(update={"execution_isolation": evidence})
        self.assertEqual(enriched.execution_isolation.workspace_scope, "RUN_DIRECTORY")
        self.assertFalse(enriched.execution_isolation.resource_limits_enforced)

        container_evidence = RunExecutionIsolationEvidence(
            mode="CONTAINER",
            executor_version="0.28.0",
            dedicated_process=True,
            credential_scope="RUN_SECRETS_ONLY",
            read_only_root_filesystem=True,
            network_policy="ALLOWLIST",
            resource_limits_enforced=True,
            runtime_image_id="sha256:" + "a" * 64,
            memory_limit_bytes=1024 * 1024 * 1024,
            cpu_limit_millis=1000,
            pids_limit=256,
        )
        self.assertEqual(container_evidence.mode, "CONTAINER")

        kubernetes_evidence = RunExecutionIsolationEvidence(
            mode="KUBERNETES",
            executor_version="0.30.0",
            dedicated_process=True,
            credential_scope="RUN_SECRETS_ONLY",
            read_only_root_filesystem=True,
            network_policy="DENY_ALL",
            resource_limits_enforced=True,
            runtime_image_id="sha256:" + "b" * 64,
            memory_limit_bytes=1024 * 1024 * 1024,
            cpu_limit_millis=1000,
            ephemeral_storage_limit_bytes=2 * 1024 * 1024 * 1024,
            orchestrator_namespace="testops-runs",
            service_account_name="testops-runner",
            service_account_token_automounted=False,
        )
        self.assertEqual(kubernetes_evidence.mode, "KUBERNETES")

        with self.assertRaisesRegex(ValueError, "default ServiceAccount"):
            RunExecutionIsolationEvidence.model_validate(
                {
                    **kubernetes_evidence.model_dump(mode="python"),
                    "service_account_name": "default",
                }
            )
        self.assertEqual(container_evidence.pids_limit, 256)

        with self.assertRaisesRegex(ValueError, "immutable image and exact limits"):
            RunExecutionIsolationEvidence(
                mode="CONTAINER",
                executor_version="0.28.0",
                dedicated_process=True,
                credential_scope="RUN_SECRETS_ONLY",
                read_only_root_filesystem=True,
                network_policy="DENY_ALL",
                resource_limits_enforced=True,
            )


if __name__ == "__main__":
    unittest.main()
