from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from testops.runners.web import load_run_snapshot
from testops.runners.web.engine import WebCaseEngine

ROOT = Path(__file__).resolve().parents[2]


def runner_digest() -> str:
    source_root = ROOT / "runners/web_playwright/src"
    digest = hashlib.sha256()
    for path in sorted(source_root.rglob("*.py")):
        digest.update(path.relative_to(source_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


class RunnerLoaderTests(unittest.TestCase):
    def test_fixture_is_a_valid_immutable_job(self) -> None:
        job = load_run_snapshot(ROOT / "tests/fixtures/run_snapshot.valid.json")
        self.assertEqual(job.case_baseline.version, "case-v1.0.0")
        self.assertEqual(job.automation_package.name, "yanjia-web")
        self.assertEqual(len(job.cases), 1)

    def test_login_smoke_job_selects_supported_cases_from_hardened_baseline(self) -> None:
        job = load_run_snapshot(ROOT / "examples/jobs/yanjia-login-smoke.json")
        self.assertEqual(job.case_baseline.version, "case-v1.0.1")
        self.assertEqual(len(job.cases), 7)
        self.assertEqual({case.module_key for case in job.cases}, {"login"})
        self.assertEqual(job.automation_package.digest, runner_digest())
        for case in job.cases:
            WebCaseEngine().validate_case(case)
        dumped = job.model_dump_json()
        self.assertNotIn("super-secret", dumped)
        self.assertIn("secret://yanjia/staging/test-password", dumped)


if __name__ == "__main__":
    unittest.main()
