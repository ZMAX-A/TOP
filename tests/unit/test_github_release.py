from __future__ import annotations

import json
import unittest

import httpx

from testops.api.config import Settings
from testops.api.github_release import HttpxGitHubReleaseFetcher, parse_github_repository
from testops.api.schemas import GitReleaseSyncRequest
from testops.api.services import InvalidRequest


class GitHubReleaseFetcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_annotated_tag_and_downloads_named_release_asset(self) -> None:
        manifest = {
            "schema_version": "1.0",
            "source": {
                "repository_url": "https://github.com/ZMAX-A/web",
                "tag": "v1.0.2",
                "revision": "a" * 40,
            },
            "baseline": {
                "schema_version": "1.0",
                "baseline_id": "8865cdb1-fc2d-5272-82bb-9d67faa89c57",
                "project_key": "yanjia-ai-web",
                "version": "case-v1.0.2",
                "source": {
                    "kind": "legacy_excel",
                    "file_name": "test_case.xlsx",
                    "file_digest": "sha256:" + "c" * 64,
                    "worksheet": "自动化测试用例",
                },
                "cases": [
                    {
                        "schema_version": "1.0",
                        "case_id": "bc082bdb-2aa8-5be8-b239-b5311167aba3",
                        "case_code": "TC-LOGIN-001",
                        "module_key": "login",
                        "module_name": "账号登录",
                        "title": "登录",
                        "test_point": "登录",
                        "priority": "P0",
                        "steps": [{"operation": "nav", "input": "/login"}],
                        "assertion": {"type": "url_contains", "expected": "/home"},
                    }
                ],
            },
            "baseline_digest": "sha256:" + "d" * 64,
            "automation_package": {
                "name": "yanjia-web-pytest",
                "version": "1.0.2",
                "digest": "sha256:" + "e" * 64,
                "runner_type": "WEB_PLAYWRIGHT",
                "image_repository": "ghcr.io/zmax-a/web",
            },
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/git/ref/tags/v1.0.2"):
                return httpx.Response(
                    200,
                    json={"object": {"type": "tag", "sha": "b" * 40}},
                )
            if path.endswith("/git/tags/" + "b" * 40):
                return httpx.Response(
                    200,
                    json={"object": {"type": "commit", "sha": "a" * 40}},
                )
            if path.endswith("/releases/tags/v1.0.2"):
                return httpx.Response(
                    200,
                    json={
                        "tag_name": "v1.0.2",
                        "draft": False,
                        "assets": [
                            {"id": 42, "name": "testops-release-manifest.json"}
                        ]
                    },
                )
            if path.endswith("/releases/assets/42"):
                return httpx.Response(
                    302,
                    headers={"Location": "https://objects.githubusercontent.com/asset"},
                )
            if request.url.host == "objects.githubusercontent.com":
                return httpx.Response(200, content=json.dumps(manifest).encode())
            return httpx.Response(404)

        fetcher = HttpxGitHubReleaseFetcher(
            Settings(),
            transport=httpx.MockTransport(handler),
        )
        result = await fetcher.fetch(
            GitReleaseSyncRequest(
                repository_url="https://github.com/ZMAX-A/web.git",
                tag="v1.0.2",
            )
        )
        self.assertEqual(result.revision, "a" * 40)
        self.assertEqual(result.repository_url, "https://github.com/ZMAX-A/web")
        self.assertEqual(result.manifest.automation_package.digest, "sha256:" + "e" * 64)

    def test_rejects_non_github_repository_urls(self) -> None:
        with self.assertRaises(InvalidRequest):
            parse_github_repository("https://example.invalid/ZMAX-A/web")
