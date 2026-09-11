from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from testops.api.config import Settings
from testops.api.github_release import FetchedGitRelease
from testops.api.main import create_app
from testops.api.schemas import GitReleaseManifest, GitReleaseSyncRequest
from testops.contracts import CaseBaseline, canonical_sha256

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_TOKEN = "git-release-bootstrap-token"
ADMIN_PASSWORD = "git-release-admin-password"
REVISION = "a" * 40


class StaticGitReleaseFetcher:
    def __init__(self, fetched: FetchedGitRelease) -> None:
        self.fetched = fetched
        self.requests: list[GitReleaseSyncRequest] = []

    async def fetch(self, request: GitReleaseSyncRequest) -> FetchedGitRelease:
        self.requests.append(request)
        return self.fetched


class GitReleaseSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "release-sync.sqlite3"
        baseline_directory = ROOT / "baselines/yanjia-ai-web/case-v1.0.1"
        self.base_document = json.loads(
            (baseline_directory / "case-baseline.json").read_text("utf-8")
        )
        self.base_digest = canonical_sha256(CaseBaseline.model_validate(self.base_document))

        base = CaseBaseline.model_validate(self.base_document)
        first_case = base.cases[0]
        candidate = base.model_copy(
            update={
                "baseline_id": uuid4(),
                "version": "case-v1.0.2",
                "cases": (
                    first_case.model_copy(update={"title": f"{first_case.title}（Release）"}),
                    *base.cases[1:],
                ),
            }
        )
        manifest = GitReleaseManifest.model_validate(
            {
                "schema_version": "1.0",
                "source": {
                    "repository_url": "https://github.com/ZMAX-A/web",
                    "tag": "v1.0.2",
                    "revision": REVISION,
                },
                "baseline": candidate.model_dump(mode="json", exclude_none=True),
                "baseline_digest": canonical_sha256(candidate),
                "automation_package": {
                    "name": "yanjia-web-pytest",
                    "version": "1.0.2",
                    "digest": "sha256:" + "b" * 64,
                    "runner_type": "WEB_PLAYWRIGHT",
                    "image_repository": "ghcr.io/zmax-a/web",
                },
            }
        )
        self.fetcher = StaticGitReleaseFetcher(
            FetchedGitRelease(
                repository_url="https://github.com/ZMAX-A/web",
                tag="v1.0.2",
                revision=REVISION,
                manifest=manifest,
            )
        )
        settings = Settings(
            database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
            auto_create_schema=True,
            bootstrap_admin_token=BOOTSTRAP_TOKEN,
        )
        self.app = create_app(settings, git_release_fetcher=self.fetcher)
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        bootstrap = self.client.post(
            "/api/v1/auth/bootstrap",
            headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
            json={
                "username": "admin",
                "display_name": "Git Release Admin",
                "password": ADMIN_PASSWORD,
            },
        )
        self.assertEqual(bootstrap.status_code, 201, bootstrap.text)
        login = self.client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": ADMIN_PASSWORD},
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        project = self.client.post(
            "/api/v1/projects",
            headers=self.headers,
            json={"key": "yanjia-ai-web", "name": "颜佳 AI Web"},
        )
        self.assertEqual(project.status_code, 201, project.text)
        self.project_id = project.json()["id"]
        target = self.client.post(
            f"/api/v1/projects/{self.project_id}/targets",
            headers=self.headers,
            json={
                "key": "web",
                "name": "Web 管理端",
                "target_type": "WEB",
                "browser": "chromium",
            },
        )
        self.assertEqual(target.status_code, 201, target.text)
        self.target_id = target.json()["id"]
        baseline = self.client.post(
            f"/api/v1/projects/{self.project_id}/baselines",
            headers=self.headers,
            json={"baseline": self.base_document, "digest": self.base_digest},
        )
        self.assertEqual(baseline.status_code, 201, baseline.text)
        self.base_baseline_id = baseline.json()["baseline_id"]
        package = self.client.post(
            f"/api/v1/projects/{self.project_id}/targets/{self.target_id}"
            "/automation-packages",
            headers=self.headers,
            json={
                "name": "yanjia-web-pytest",
                "version": "1.0.1",
                "digest": "sha256:" + "1" * 64,
                "image_repository": "ghcr.io/zmax-a/web",
            },
        )
        self.assertEqual(package.status_code, 201, package.text)
        self.previous_package_id = package.json()["id"]
        self.release_url = (
            f"/api/v1/projects/{self.project_id}/targets/{self.target_id}"
            "/automation-packages/git-release"
        )

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    def test_preview_and_apply_create_governed_drafts_idempotently(self) -> None:
        source = {
            "repository_url": "https://github.com/ZMAX-A/web",
            "tag": "v1.0.2",
        }
        preview_response = self.client.post(
            f"{self.release_url}/preview",
            headers=self.headers,
            json=source,
        )
        self.assertEqual(preview_response.status_code, 200, preview_response.text)
        preview = preview_response.json()
        self.assertEqual(preview["revision"], REVISION)
        self.assertEqual(preview["base_baseline_id"], self.base_baseline_id)
        self.assertEqual(preview["modified_count"], 1)
        self.assertEqual(preview["added_count"], 0)
        self.assertEqual(preview["deleted_count"], 0)
        self.assertTrue(preview["baseline_changed"])
        self.assertEqual(preview["supersedes_id"], self.previous_package_id)

        apply_payload = {
            **source,
            "expected_revision": preview["revision"],
            "expected_baseline_digest": preview["candidate_baseline_digest"],
            "expected_package_digest": preview["automation_package"]["digest"],
        }
        applied_response = self.client.post(
            f"{self.release_url}/apply",
            headers=self.headers,
            json=apply_payload,
        )
        self.assertEqual(applied_response.status_code, 201, applied_response.text)
        applied = applied_response.json()
        self.assertTrue(applied["baseline_changed"])
        self.assertIsNotNone(applied["change_request_id"])
        self.assertEqual(applied["baseline_id"], preview["candidate_baseline_id"])
        package = applied["automation_package"]
        self.assertEqual(package["status"], "DRAFT")
        self.assertEqual(package["supply_chain_status"], "PENDING")
        self.assertEqual(package["supersedes_id"], self.previous_package_id)

        change = self.client.get(
            f"/api/v1/projects/{self.project_id}/change-requests/"
            f"{applied['change_request_id']}",
            headers=self.headers,
        )
        self.assertEqual(change.status_code, 200, change.text)
        self.assertEqual(change.json()["candidate_digest"], preview["candidate_baseline_digest"])
        self.assertEqual(change.json()["changes"][0]["change_type"], "MODIFY")

        retried_response = self.client.post(
            f"{self.release_url}/apply",
            headers=self.headers,
            json=apply_payload,
        )
        self.assertEqual(retried_response.status_code, 201, retried_response.text)
        self.assertEqual(retried_response.json()["change_request_id"], applied["change_request_id"])
        self.assertEqual(
            retried_response.json()["automation_package"]["id"],
            package["id"],
        )

    def test_apply_rejects_preview_digest_mismatch(self) -> None:
        response = self.client.post(
            f"{self.release_url}/apply",
            headers=self.headers,
            json={
                "repository_url": "https://github.com/ZMAX-A/web",
                "tag": "v1.0.2",
                "expected_revision": REVISION,
                "expected_baseline_digest": "sha256:" + "0" * 64,
                "expected_package_digest": "sha256:" + "b" * 64,
            },
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("baseline digest changed", response.json()["detail"])

    def test_baseline_only_release_reuses_existing_immutable_package(self) -> None:
        manifest = self.fetcher.fetched.manifest.model_copy(
            update={
                "automation_package": self.fetcher.fetched.manifest.automation_package.model_copy(
                    update={
                        "version": "1.0.1",
                        "digest": "sha256:" + "1" * 64,
                    }
                )
            }
        )
        self.fetcher.fetched = FetchedGitRelease(
            repository_url=self.fetcher.fetched.repository_url,
            tag=self.fetcher.fetched.tag,
            revision=self.fetcher.fetched.revision,
            manifest=manifest,
        )
        source = {
            "repository_url": "https://github.com/ZMAX-A/web",
            "tag": "v1.0.2",
        }
        preview_response = self.client.post(
            f"{self.release_url}/preview",
            headers=self.headers,
            json=source,
        )
        self.assertEqual(preview_response.status_code, 200, preview_response.text)
        preview = preview_response.json()

        applied_response = self.client.post(
            f"{self.release_url}/apply",
            headers=self.headers,
            json={
                **source,
                "expected_revision": preview["revision"],
                "expected_baseline_digest": preview["candidate_baseline_digest"],
                "expected_package_digest": preview["automation_package"]["digest"],
            },
        )
        self.assertEqual(applied_response.status_code, 201, applied_response.text)
        applied = applied_response.json()
        self.assertEqual(applied["automation_package"]["id"], self.previous_package_id)
        self.assertEqual(applied["automation_package"]["status"], "ACTIVE")
        self.assertEqual(applied["automation_package"]["supply_chain_status"], "LEGACY")

        change = self.client.get(
            f"/api/v1/projects/{self.project_id}/change-requests/"
            f"{applied['change_request_id']}",
            headers=self.headers,
        )
        self.assertEqual(change.status_code, 200, change.text)
        self.assertEqual(change.json()["validation_status"], "NOT_REQUIRED")
