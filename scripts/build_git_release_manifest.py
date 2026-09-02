"""Build the immutable release asset consumed by the TestOps Git sync API."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

from testops.contracts import (
    AutomationPackageRef,
    CaseBaseline,
    canonical_json_bytes,
    canonical_sha256,
)

TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class ReleaseManifestError(ValueError):
    """The release inputs cannot form a trusted TestOps manifest."""


def _repository_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or len(parts) != 2
    ):
        raise ReleaseManifestError("repository URL must identify one HTTPS GitHub repository")
    owner, name = parts
    if name.endswith(".git"):
        name = name[:-4]
    if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", part) for part in (owner, name)):
        raise ReleaseManifestError("repository owner or name is invalid")
    return f"https://github.com/{owner}/{name}"


def build_release_manifest(
    *,
    baseline_path: Path,
    baseline_manifest_path: Path,
    repository_url: str,
    tag: str,
    revision: str,
    package_name: str,
    package_version: str,
    package_digest: str,
    image_repository: str,
) -> dict[str, object]:
    if not TAG_PATTERN.fullmatch(tag):
        raise ReleaseManifestError("tag has an invalid format")
    if not REVISION_PATTERN.fullmatch(revision):
        raise ReleaseManifestError("revision must be a lowercase 40-character Git commit SHA")
    baseline = CaseBaseline.model_validate_json(baseline_path.read_bytes())
    baseline_digest = canonical_sha256(baseline)
    baseline_manifest = json.loads(baseline_manifest_path.read_text("utf-8"))
    recorded_digest = baseline_manifest.get("baseline", {}).get("digest")
    if recorded_digest != baseline_digest:
        raise ReleaseManifestError("baseline manifest digest does not match case-baseline.json")
    package = AutomationPackageRef(
        name=package_name,
        version=package_version,
        digest=package_digest,
        runner_type="WEB_PLAYWRIGHT",
        image_repository=image_repository,
    )
    return {
        "schema_version": "1.0",
        "source": {
            "repository_url": _repository_url(repository_url),
            "tag": tag,
            "revision": revision,
        },
        "baseline": baseline.model_dump(mode="json", exclude_none=True),
        "baseline_digest": baseline_digest,
        "automation_package": package.model_dump(
            mode="json",
            exclude_none=True,
            exclude={"supply_chain"},
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build an immutable TestOps GitHub Release manifest"
    )
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--baseline-manifest", required=True, type=Path)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--package-name", required=True)
    parser.add_argument("--package-version", required=True)
    parser.add_argument("--package-digest", required=True)
    parser.add_argument("--image-repository", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.output.exists():
        raise ReleaseManifestError("output already exists; release manifests are immutable")
    document = build_release_manifest(
        baseline_path=args.baseline,
        baseline_manifest_path=args.baseline_manifest,
        repository_url=args.repository_url,
        tag=args.tag,
        revision=args.revision,
        package_name=args.package_name,
        package_version=args.package_version,
        package_digest=args.package_digest,
        image_repository=args.image_repository,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json_bytes(document))
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "tag": document["source"]["tag"],
                "revision": document["source"]["revision"],
                "baseline_digest": document["baseline_digest"],
                "package_digest": document["automation_package"]["digest"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
