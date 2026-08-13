"""Contract-driven browser steps for the first Web Runner vertical slice."""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from testops.contracts import CaseDefinition, StepDefinition, WebRunConfig

from .variables import VariableResolver


class RunnerJobValidationError(ValueError):
    """The job uses behavior that this Runner version does not support."""


class StepExecutionError(RuntimeError):
    """A browser action could not be completed."""


class AssertionExecutionError(AssertionError):
    """A case assertion failed."""


class WebCaseEngine:
    """Execute the login-module capability slice against a Playwright-like page."""

    SUPPORTED_OPERATIONS = {"input", "click"}
    SUPPORTED_ASSERTIONS = {
        "text_contains",
        "text_visible",
        "url_contains",
        "url_not_contains",
    }

    def validate_case(self, case: CaseDefinition) -> None:
        unsupported_operations = sorted(
            {step.operation for step in case.steps} - self.SUPPORTED_OPERATIONS
        )
        if unsupported_operations:
            raise RunnerJobValidationError(
                f"case {case.case_code} uses unsupported operations: "
                f"{', '.join(unsupported_operations)}"
            )
        if case.assertion.type not in self.SUPPORTED_ASSERTIONS:
            raise RunnerJobValidationError(
                f"case {case.case_code} uses unsupported assertion: {case.assertion.type}"
            )
        for precondition in case.preconditions:
            if "打开登录" not in precondition and "未登录" not in precondition:
                raise RunnerJobValidationError(
                    f"case {case.case_code} uses unsupported precondition"
                )
        if case.assertion.type == "url_contains":
            keywords = self._url_keywords(str(case.assertion.expected or ""))
            if keywords == ["/"]:
                raise RunnerJobValidationError(
                    f"case {case.case_code} has weak URL assertion '/' that also matches /login"
                )

    def execute_case(
        self,
        page: Any,
        case: CaseDefinition,
        config: WebRunConfig,
        resolver: VariableResolver,
    ) -> None:
        self._apply_preconditions(page, case, config)
        timeout_ms = round((case.timeout_seconds or config.action_timeout_seconds) * 1000)
        for step in case.steps:
            self._execute_step(page, step, resolver, timeout_ms)
        self._assert_case(page, case, timeout_ms)

    @staticmethod
    def _login_url(base_url: str) -> str:
        parts = urlsplit(base_url)
        path = parts.path.rstrip("/")
        if path.endswith("/login"):
            return base_url
        return urlunsplit(
            (parts.scheme, parts.netloc, f"{path}/login" if path else "/login", "", "")
        )

    def _apply_preconditions(
        self,
        page: Any,
        case: CaseDefinition,
        config: WebRunConfig,
    ) -> None:
        if not case.preconditions:
            return
        login_url = self._login_url(config.base_url)
        for precondition in case.preconditions:
            if "打开登录" in precondition or "未登录" in precondition:
                page.goto(
                    login_url,
                    wait_until="domcontentloaded",
                    timeout=round(config.navigation_timeout_seconds * 1000),
                )

    def _execute_step(
        self,
        page: Any,
        step: StepDefinition,
        resolver: VariableResolver,
        timeout_ms: int,
    ) -> None:
        if step.operation == "input":
            self._input(page, step, resolver, timeout_ms)
            return
        if step.operation == "click":
            self._click(page, step, timeout_ms)
            return
        raise StepExecutionError(f"unsupported operation at execution: {step.operation}")

    @staticmethod
    def _input(
        page: Any,
        step: StepDefinition,
        resolver: VariableResolver,
        timeout_ms: int,
    ) -> None:
        locator = step.locator or ""
        if not locator:
            raise StepExecutionError("input requires locator")
        value = resolver.resolve_text(str(step.input if step.input is not None else ""))
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                element = page.locator(locator).first
                element.wait_for(state="visible", timeout=timeout_ms)
                element.fill(value, timeout=timeout_ms)
                return
            except Exception as exc:  # Playwright exceptions are optional imports here.
                last_error = exc
                if attempt == 0:
                    page.wait_for_timeout(500)
        raise StepExecutionError(
            f"input failed for locator {locator}: {last_error}"
        ) from last_error

    def _click(self, page: Any, step: StepDefinition, timeout_ms: int) -> None:
        locator = step.locator or ""
        if not locator:
            raise StepExecutionError("click requires locator")
        last_error: Exception | None = None
        element: Any = None
        for attempt in range(2):
            try:
                element = page.locator(locator).first
                element.wait_for(state="visible", timeout=timeout_ms)
                element.click(timeout=timeout_ms)
                self._after_click(page, locator, timeout_ms)
                return
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                    page.wait_for_timeout(300)
        try:
            element = page.locator(locator).first
            element.click(force=True, timeout=timeout_ms)
            self._after_click(page, locator, timeout_ms)
            return
        except Exception as exc:
            last_error = exc
        raise StepExecutionError(
            f"click failed for locator {locator}: {last_error}"
        ) from last_error

    @staticmethod
    def _after_click(page: Any, locator: str, timeout_ms: int) -> None:
        page.wait_for_timeout(300)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass

        if "store" not in locator.lower():
            return
        page.wait_for_timeout(300)
        options = page.locator(".ant-select-item-option")
        if options.count() > 0:
            options.first.click(timeout=timeout_ms)
        else:
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
        page.keyboard.press("Escape")

    def _assert_case(self, page: Any, case: CaseDefinition, timeout_ms: int) -> None:
        assertion = case.assertion
        expected = str(assertion.expected or "")
        if assertion.type == "text_visible":
            self._assert_text_visible(page, expected, timeout_ms)
            return
        if assertion.type == "text_contains":
            self._assert_text_contains(page, expected, assertion.locator, timeout_ms)
            return
        if assertion.type in {"url_contains", "url_not_contains"}:
            self._assert_url(page, expected, assertion.type == "url_not_contains")
            return
        raise AssertionExecutionError(f"unsupported assertion: {assertion.type}")

    @classmethod
    def _assert_text_visible(cls, page: Any, expected: str, timeout_ms: int) -> None:
        keywords = cls._multi_keywords(expected)
        if not keywords:
            raise AssertionExecutionError("text_visible has no expected text")
        body_text = ""
        attempts = max(1, math.ceil(timeout_ms / 250))
        for _ in range(attempts):
            body_text = page.locator("body").inner_text()
            if all(keyword in body_text for keyword in keywords):
                return
            page.wait_for_timeout(250)
        missing = [keyword for keyword in keywords if keyword not in body_text]
        raise AssertionExecutionError(f"page is missing expected text: {missing}")

    @classmethod
    def _assert_text_contains(
        cls,
        page: Any,
        expected: str,
        locator: str | None,
        timeout_ms: int,
    ) -> None:
        keyword = cls._single_keyword(expected)
        if not keyword:
            raise AssertionExecutionError("text_contains has no expected text")
        if locator:
            try:
                actual = page.locator(locator).first.inner_text(timeout=min(timeout_ms, 2000))
                if keyword in actual:
                    return
            except Exception:
                pass
        body_text = page.locator("body").inner_text()
        if keyword not in body_text:
            raise AssertionExecutionError(f"page text does not contain expected keyword: {keyword}")

    @classmethod
    def _assert_url(cls, page: Any, expected: str, negate: bool) -> None:
        keywords = cls._url_keywords(expected)
        if not keywords:
            raise AssertionExecutionError("URL assertion has no parseable expected value")
        url = page.url
        if negate:
            present = [keyword for keyword in keywords if keyword in url]
            if present:
                raise AssertionExecutionError(f"URL still contains forbidden values: {present}")
            return
        missing = [keyword for keyword in keywords if keyword not in url]
        if missing:
            raise AssertionExecutionError(f"URL does not contain expected values: {missing}")

    @staticmethod
    def _single_keyword(text: str) -> str:
        if not text:
            return ""
        match = re.search(r"['\"]([^'\"]*)['\"]", text.strip())
        return match.group(1).strip() if match else text[:40].strip()

    @staticmethod
    def _multi_keywords(text: str) -> list[str]:
        if not text:
            return []
        keywords = re.findall(r"['\"]([^'\"]+)['\"]", text)
        if keywords:
            return keywords
        return [part.strip() for part in re.split(r"[、，,]", text) if part.strip()]

    @staticmethod
    def _url_keywords(text: str) -> list[str]:
        if text.strip() == "/":
            return ["/"]
        keywords = re.findall(r"['\"]([^'\"]+)['\"]", text)
        if keywords:
            return keywords
        return re.findall(r"/[\w-]+", text)
