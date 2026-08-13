"""Launch Chromium and verify a local in-memory page without network access."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
for relative in ("packages/contracts/src", "runners/web_playwright/src"):
    sys.path.insert(0, str(ROOT / relative))

from testops.runners.web.browser_backend import PlaywrightBrowserBackend  # noqa: E402
from testops.runners.web.job_loader import load_run_snapshot  # noqa: E402
from testops.runners.web.workspace import RunWorkspace  # noqa: E402


def main() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            browser_version = browser.version
            page = browser.new_page()
            page.set_content(
                "<html><head><title>TestOps Runner Smoke</title></head>"
                "<body><button id='ready'>ready</button></body></html>"
            )
            if page.title() != "TestOps Runner Smoke":
                raise RuntimeError("unexpected browser page title")
            if not page.locator("#ready").is_visible():
                raise RuntimeError("browser did not render the smoke element")
        finally:
            browser.close()

    job = load_run_snapshot(ROOT / "tests/fixtures/run_snapshot.valid.json")
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = RunWorkspace(temporary_directory, job.run_id)
        backend = PlaywrightBrowserBackend()
        with backend.start(job, workspace) as browser_run:
            with browser_run.case_page("BACKEND-SMOKE") as page:
                page.set_content("<main id='backend-ready'>backend ready</main>")
                if not page.locator("#backend-ready").is_visible():
                    raise RuntimeError("Web Runner backend did not render the smoke element")
    print(
        json.dumps(
            {
                "status": "passed",
                "browser": "chromium",
                "browser_version": browser_version,
                "runner_backend_lifecycle": "passed",
                "network_used": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
