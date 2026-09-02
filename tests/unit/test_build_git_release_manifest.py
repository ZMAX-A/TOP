from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_git_release_manifest import ReleaseManifestError, build_release_manifest

ROOT = Path(__file__).resolve().parents[2]


class BuildGitReleaseManifestTests(unittest.TestCase):
    def test_builds_server_compatible_release_manifest(self) -> None:
        baseline_directory = ROOT / "baselines/yanjia-ai-web/case-v1.0.1"
        document = build_release_manifest(
            baseline_path=baseline_directory / "case-baseline.json",
            baseline_manifest_path=baseline_directory / "manifest.json",
            repository_url="https://github.com/ZMAX-A/web.git",
            tag="v1.0.2",
            revision="a" * 40,
            package_name="yanjia-web-pytest",
            package_version="1.0.2",
            package_digest="sha256:" + "b" * 64,
            image_repository="ghcr.io/zmax-a/web",
        )
        self.assertEqual(document["source"]["repository_url"], "https://github.com/ZMAX-A/web")
        self.assertEqual(document["baseline"]["version"], "case-v1.0.1")
        self.assertEqual(document["automation_package"]["digest"], "sha256:" + "b" * 64)

    def test_rejects_mismatched_baseline_manifest_digest(self) -> None:
        baseline_directory = ROOT / "baselines/yanjia-ai-web/case-v1.0.1"
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            manifest_path.write_text(
                json.dumps({"baseline": {"digest": "sha256:" + "0" * 64}}),
                encoding="utf-8",
            )
            with self.assertRaises(ReleaseManifestError):
                build_release_manifest(
                    baseline_path=baseline_directory / "case-baseline.json",
                    baseline_manifest_path=manifest_path,
                    repository_url="https://github.com/ZMAX-A/web",
                    tag="v1.0.2",
                    revision="a" * 40,
                    package_name="yanjia-web-pytest",
                    package_version="1.0.2",
                    package_digest="sha256:" + "b" * 64,
                    image_repository="ghcr.io/zmax-a/web",
                )
