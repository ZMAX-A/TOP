"""Optional Playwright browser backend kept behind an import-safe protocol."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Protocol

from testops.contracts import RunSnapshot

from .workspace import RunWorkspace


class BrowserRun(Protocol):
    def case_page(self, case_code: str) -> AbstractContextManager[Any]:
        """Return an isolated page for one case."""


class BrowserBackend(Protocol):
    def start(
        self, job: RunSnapshot, workspace: RunWorkspace
    ) -> AbstractContextManager[BrowserRun]:
        """Start one browser process for a run."""

    def health(self) -> dict[str, object]:
        """Return dependency availability without launching a browser."""


class PlaywrightBrowserRun:
    def __init__(self, browser: Any, job: RunSnapshot, workspace: RunWorkspace):
        self._browser = browser
        self._job = job
        self._workspace = workspace

    @contextmanager
    def case_page(self, case_code: str) -> Iterator[Any]:
        config = self._job.web_config
        if config is None:  # guarded by the contract and adapter validation
            raise RuntimeError("WEB job has no web_config")
        context = self._browser.new_context(
            viewport={"width": config.viewport_width, "height": config.viewport_height},
            ignore_https_errors=config.ignore_https_errors,
        )
        trace_started = False
        if config.capture_trace:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            trace_started = True
        page = context.new_page()
        page.set_default_timeout(round(config.action_timeout_seconds * 1000))
        page.set_default_navigation_timeout(round(config.navigation_timeout_seconds * 1000))
        try:
            yield page
        except Exception:
            screenshot = self._workspace.artifact_path("screenshots", f"{case_code}.png")
            try:
                page.screenshot(path=str(screenshot), full_page=True)
            except Exception:
                pass
            if trace_started:
                trace = self._workspace.artifact_path("traces", f"{case_code}.zip")
                try:
                    context.tracing.stop(path=str(trace))
                except Exception:
                    pass
                trace_started = False
            raise
        finally:
            if trace_started:
                try:
                    context.tracing.stop()
                except Exception:
                    pass
            context.close()


class PlaywrightBrowserBackend:
    @contextmanager
    def start(self, job: RunSnapshot, workspace: RunWorkspace) -> Iterator[PlaywrightBrowserRun]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed; install the project runner dependency"
            ) from exc

        manager = sync_playwright()
        playwright = manager.start()
        browser = None
        try:
            browser_name = job.browser or ""
            if browser_name not in {"chromium", "firefox", "webkit"}:
                raise RuntimeError(f"unsupported browser: {browser_name}")
            browser_type = getattr(playwright, browser_name)
            config = job.web_config
            browser = browser_type.launch(headless=True if config is None else config.headless)
            yield PlaywrightBrowserRun(browser, job, workspace)
        finally:
            try:
                if browser is not None:
                    browser.close()
            finally:
                playwright.stop()

    def health(self) -> dict[str, object]:
        try:
            import playwright
        except ImportError:
            return {"available": False, "reason": "playwright package is not installed"}
        return {
            "available": True,
            "package": playwright.__name__,
            "browser_launch_checked": False,
        }
