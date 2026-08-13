from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from testops.contracts import (
    AssertionDefinition,
    AutomationPackageRef,
    CaseBaselineRef,
    CaseDefinition,
    CasePriority,
    CaseResultStatus,
    RunSnapshot,
    RunStatus,
    RuntimeVariable,
    SecretBinding,
    StepDefinition,
    TargetType,
    WebRunConfig,
)
from testops.runners.web.engine import RunnerJobValidationError, WebCaseEngine
from testops.runners.web.playwright_adapter import PlaywrightWebAdapter
from testops.runners.web.variables import (
    EnvironmentSecretProvider,
    MappingSecretProvider,
    SecretResolutionError,
    VariableResolver,
)
from testops.runners.web.workspace import RunWorkspace, WorkspaceError

DIGEST = "sha256:" + "b" * 64


class FakeKeyboard:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def press(self, key: str) -> None:
        self.keys.append(key)


class FakeLocator:
    def __init__(self, page: FakePage, selector: str):
        self.page = page
        self.selector = selector

    @property
    def first(self) -> FakeLocator:
        return self

    def count(self) -> int:
        return 1

    def wait_for(self, **_kwargs: Any) -> None:
        return None

    def fill(self, value: str, **_kwargs: Any) -> None:
        self.page.fields[self.selector] = value

    def click(self, **_kwargs: Any) -> None:
        if self.selector == ".ant-select-item-option":
            self.page.store_selected = True
            return
        if "用户协议" in self.selector:
            self.page.url = "https://example.invalid/user-agreemen"
            return
        if "submit" not in self.selector:
            return
        username = self.page.fields.get("#username", "")
        password = self.page.fields.get("#password", "")
        if not username:
            self.page.body_text = "请输入账号"
        elif not password:
            self.page.body_text = "请输入密码"
        elif not self.page.store_selected:
            self.page.body_text = "请选择门店"
        elif username == "good-user" and password == "super-secret":
            self.page.url = "https://example.invalid/"
            self.page.body_text = "首页"
        else:
            self.page.body_text = "登录失败"

    def inner_text(self, **_kwargs: Any) -> str:
        if self.selector == "body":
            return self.page.body_text
        return ""


class FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.body_text = ""
        self.fields: dict[str, str] = {}
        self.store_selected = False
        self.keyboard = FakeKeyboard()

    def goto(self, url: str, **_kwargs: Any) -> None:
        self.url = url
        self.body_text = "登录"

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def wait_for_load_state(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class FakeBrowserRun:
    def __init__(self) -> None:
        self.pages: list[FakePage] = []

    @contextmanager
    def case_page(self, _case_code: str):
        page = FakePage()
        self.pages.append(page)
        yield page


class FakeBrowserBackend:
    def __init__(self) -> None:
        self.run = FakeBrowserRun()

    @contextmanager
    def start(self, _job: RunSnapshot, _workspace: RunWorkspace):
        yield self.run

    def health(self) -> dict[str, object]:
        return {"available": True, "fake": True}


def login_case(
    *,
    assertion: AssertionDefinition | None = None,
    enabled: bool = True,
) -> CaseDefinition:
    return CaseDefinition(
        case_id=UUID("11000000-0000-0000-0000-000000000001"),
        case_code="TC-LOGIN-007",
        module_key="login",
        module_name="账号登录",
        title="登录成功",
        test_point="验证登录后离开登录页",
        priority=CasePriority.P0,
        preconditions=("打开登录页面",),
        steps=(
            StepDefinition(operation="input", locator="#username", input="${TEST_USERNAME}"),
            StepDefinition(operation="input", locator="#password", input="${TEST_PASSWORD}"),
            StepDefinition(operation="click", locator="#store"),
            StepDefinition(operation="click", locator="button[type='submit']"),
        ),
        assertion=assertion or AssertionDefinition(type="url_not_contains", expected="/login"),
        tags=("smoke",),
        enabled=enabled,
    )


def login_job(case: CaseDefinition | None = None) -> RunSnapshot:
    return RunSnapshot(
        run_id=UUID("22000000-0000-0000-0000-000000000001"),
        project_id=UUID("33000000-0000-0000-0000-000000000001"),
        target_id=UUID("44000000-0000-0000-0000-000000000001"),
        target_type=TargetType.WEB,
        environment_id=UUID("55000000-0000-0000-0000-000000000001"),
        case_baseline=CaseBaselineRef(
            baseline_id=UUID("66000000-0000-0000-0000-000000000001"),
            version="case-v1.0.1",
            digest=DIGEST,
            case_count=92,
        ),
        automation_package=AutomationPackageRef(
            name="yanjia-web",
            version="0.1.0",
            digest=DIGEST,
        ),
        cases=(case or login_case(),),
        browser="chromium",
        web_config=WebRunConfig(base_url="https://example.invalid/login", capture_trace=False),
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
        created_by=UUID("77000000-0000-0000-0000-000000000001"),
        created_at=datetime(2026, 8, 11, 16, 0, tzinfo=UTC),
    )


def secret_provider() -> MappingSecretProvider:
    return MappingSecretProvider(
        {
            "secret://yanjia/staging/test-username": "good-user",
            "secret://yanjia/staging/test-password": "super-secret",
        }
    )


class VariableResolverTests(unittest.TestCase):
    def test_secret_values_resolve_and_are_redacted(self) -> None:
        resolver = VariableResolver(login_job(), secret_provider())
        self.assertEqual(resolver.resolve_text("${TEST_PASSWORD}"), "super-secret")
        self.assertEqual(resolver.redact("failed: super-secret"), "failed: ***")
        self.assertNotIn("super-secret", repr(resolver))

    def test_environment_provider_reports_name_but_not_value(self) -> None:
        provider = EnvironmentSecretProvider(environ={})
        with self.assertRaisesRegex(SecretResolutionError, "TESTOPS_SECRET_TEST_USERNAME"):
            provider.resolve(login_job().secret_bindings[0])

    def test_secret_like_plain_variable_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RuntimeVariable(name="ACCESS_TOKEN", value="unsafe")


class WebCaseEngineTests(unittest.TestCase):
    def test_login_case_executes_with_store_selection(self) -> None:
        job = login_job()
        resolver = VariableResolver(job, secret_provider())
        page = FakePage()
        WebCaseEngine().execute_case(page, job.cases[0], job.web_config, resolver)
        self.assertEqual(page.url, "https://example.invalid/")
        self.assertTrue(page.store_selected)

    def test_weak_root_url_assertion_is_rejected(self) -> None:
        case = login_case(assertion=AssertionDefinition(type="url_contains", expected="/"))
        with self.assertRaisesRegex(RunnerJobValidationError, "weak URL assertion"):
            WebCaseEngine().validate_case(case)


class PlaywrightWebAdapterTests(unittest.TestCase):
    def test_adapter_returns_structured_pass_and_artifacts(self) -> None:
        events: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            backend = FakeBrowserBackend()
            adapter = PlaywrightWebAdapter(
                workspace_root=temporary_directory,
                secret_provider=secret_provider(),
                browser_backend=backend,
            )
            job = login_job()
            adapter.prepare(job)
            result = adapter.execute(job, events.append)

            self.assertEqual(result.status, RunStatus.PASSED)
            self.assertEqual(result.case_results[0].status, CaseResultStatus.PASSED)
            self.assertTrue(any(artifact.name == "events.jsonl" for artifact in result.artifacts))
            self.assertTrue(
                (Path(temporary_directory) / str(job.run_id) / "run-result.json").is_file()
            )
            self.assertEqual(
                adapter.collect(str(job.run_id)), tuple(a.uri for a in result.artifacts)
            )
            self.assertEqual(events[0]["event"], "run_started")
            self.assertEqual(events[-1]["event"], "run_finished")

    def test_adapter_reports_assertion_failure_without_secret_values(self) -> None:
        failing = login_case(
            assertion=AssertionDefinition(type="text_visible", expected="'永远不存在'")
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            adapter = PlaywrightWebAdapter(
                workspace_root=temporary_directory,
                secret_provider=secret_provider(),
                browser_backend=FakeBrowserBackend(),
            )
            result = adapter.execute(login_job(failing), lambda _event: None)

            self.assertEqual(result.status, RunStatus.FAILED)
            self.assertEqual(result.case_results[0].status, CaseResultStatus.FAILED)
            self.assertNotIn("super-secret", result.case_results[0].error_message or "")

    def test_cancel_before_execution_is_terminal_canceled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            adapter = PlaywrightWebAdapter(
                workspace_root=temporary_directory,
                secret_provider=secret_provider(),
                browser_backend=FakeBrowserBackend(),
            )
            job = login_job()
            adapter.prepare(job)
            adapter.cancel(str(job.run_id))
            result = adapter.execute(job, lambda _event: None)

            self.assertEqual(result.status, RunStatus.CANCELED)
            self.assertEqual(result.case_results[0].status, CaseResultStatus.SKIPPED)

    def test_cancel_from_case_started_event_skips_current_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            backend = FakeBrowserBackend()
            adapter = PlaywrightWebAdapter(
                workspace_root=temporary_directory,
                secret_provider=secret_provider(),
                browser_backend=backend,
            )
            job = login_job()

            def reporter(event: dict[str, object]) -> None:
                if event["event"] == "case_started":
                    adapter.cancel(str(job.run_id))

            result = adapter.execute(job, reporter)

            self.assertEqual(result.status, RunStatus.CANCELED)
            self.assertEqual(result.case_results[0].status, CaseResultStatus.SKIPPED)
            self.assertEqual(backend.run.pages, [])


class RunWorkspaceTests(unittest.TestCase):
    def test_workspace_refuses_run_id_reuse(self) -> None:
        run_id = UUID("88000000-0000-0000-0000-000000000001")
        with tempfile.TemporaryDirectory() as temporary_directory:
            RunWorkspace(temporary_directory, run_id)
            with self.assertRaisesRegex(WorkspaceError, "already exists"):
                RunWorkspace(temporary_directory, run_id)


if __name__ == "__main__":
    unittest.main()
