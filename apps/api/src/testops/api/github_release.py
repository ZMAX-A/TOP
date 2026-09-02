"""Read immutable TestOps release manifests from GitHub Releases."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, urljoin, urlsplit

import httpx
from pydantic import ValidationError

from .config import Settings
from .schemas import GitReleaseManifest, GitReleaseSyncRequest
from .services import InvalidRequest, ServiceError

_REPOSITORY_PART = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})$")
_ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)


class GitHubReleaseUnavailable(ServiceError):
    status_code = 502


@dataclass(frozen=True, slots=True)
class GitHubRepository:
    owner: str
    name: str

    @property
    def canonical_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True)
class FetchedGitRelease:
    repository_url: str
    tag: str
    revision: str
    manifest: GitReleaseManifest


class GitReleaseFetcher(Protocol):
    async def fetch(self, request: GitReleaseSyncRequest) -> FetchedGitRelease: ...


def parse_github_repository(value: str) -> GitHubRepository:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise InvalidRequest("repository_url must be an HTTPS github.com repository URL")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise InvalidRequest("repository_url must identify exactly one GitHub owner/repository")
    owner, name = parts
    if name.endswith(".git"):
        name = name[:-4]
    if not _REPOSITORY_PART.fullmatch(owner) or not _REPOSITORY_PART.fullmatch(name):
        raise InvalidRequest("repository_url contains an invalid GitHub owner or repository name")
    return GitHubRepository(owner=owner, name=name)


class HttpxGitHubReleaseFetcher:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = settings.github_release_token
        self._timeout = settings.github_release_timeout_seconds
        self._max_bytes = settings.github_release_max_manifest_bytes
        self._transport = transport

    def _headers(self, *, asset: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/octet-stream" if asset else "application/vnd.github+json",
            "User-Agent": "testops-platform-release-sync/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _github_json(self, client: httpx.AsyncClient, url: str) -> dict[str, object]:
        try:
            response = await client.get(url, headers=self._headers())
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise GitHubReleaseUnavailable("GitHub release metadata is unavailable") from exc
        if not isinstance(document, dict):
            raise GitHubReleaseUnavailable("GitHub release metadata has an invalid shape")
        return document

    async def _resolve_tag_revision(
        self,
        client: httpx.AsyncClient,
        repository: GitHubRepository,
        tag: str,
    ) -> str:
        base = f"https://api.github.com/repos/{repository.owner}/{repository.name}"
        document = await self._github_json(client, f"{base}/git/ref/tags/{quote(tag, safe='')}")
        target = document.get("object")
        for _ in range(5):
            if not isinstance(target, dict):
                break
            object_type = target.get("type")
            sha = target.get("sha")
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
                break
            if object_type == "commit":
                return sha
            if object_type != "tag":
                break
            tag_document = await self._github_json(client, f"{base}/git/tags/{sha}")
            target = tag_document.get("object")
        raise GitHubReleaseUnavailable("GitHub tag does not resolve to a commit")

    async def _download_asset(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> bytes:
        current = url
        headers = self._headers(asset=True)
        for _ in range(6):
            parsed = urlsplit(current)
            if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() not in (
                _ALLOWED_DOWNLOAD_HOSTS
            ):
                raise GitHubReleaseUnavailable("GitHub release asset redirect was rejected")
            try:
                response = await client.get(current, headers=headers, follow_redirects=False)
            except httpx.HTTPError as exc:
                raise GitHubReleaseUnavailable("GitHub release manifest is unavailable") from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise GitHubReleaseUnavailable("GitHub release asset redirect is incomplete")
                current = urljoin(current, location)
                headers = {"User-Agent": headers["User-Agent"]}
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise GitHubReleaseUnavailable("GitHub release manifest is unavailable") from exc
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise GitHubReleaseUnavailable(
                        "GitHub release manifest has an invalid Content-Length"
                    ) from exc
                if declared_length > self._max_bytes:
                    raise InvalidRequest(
                        "GitHub release manifest exceeds the configured size limit"
                    )
            if len(response.content) > self._max_bytes:
                raise InvalidRequest("GitHub release manifest exceeds the configured size limit")
            return response.content
        raise GitHubReleaseUnavailable("GitHub release asset redirected too many times")

    async def fetch(self, request: GitReleaseSyncRequest) -> FetchedGitRelease:
        repository = parse_github_repository(request.repository_url)
        base = f"https://api.github.com/repos/{repository.owner}/{repository.name}"
        timeout = httpx.Timeout(self._timeout)
        async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
            revision = await self._resolve_tag_revision(client, repository, request.tag)
            release = await self._github_json(
                client,
                f"{base}/releases/tags/{quote(request.tag, safe='')}",
            )
            if release.get("tag_name") != request.tag or release.get("draft") is not False:
                raise InvalidRequest("GitHub release must be published for the requested tag")
            assets = release.get("assets")
            if not isinstance(assets, list):
                raise GitHubReleaseUnavailable("GitHub release assets have an invalid shape")
            asset_id: int | None = None
            for item in assets:
                if isinstance(item, dict) and item.get("name") == request.asset_name:
                    raw_id = item.get("id")
                    if isinstance(raw_id, int) and raw_id > 0:
                        asset_id = raw_id
                        break
            if asset_id is None:
                raise GitHubReleaseUnavailable(
                    f"GitHub release asset was not found: {request.asset_name}"
                )
            payload = await self._download_asset(client, f"{base}/releases/assets/{asset_id}")

        try:
            manifest = GitReleaseManifest.model_validate_json(payload)
        except ValidationError as exc:
            raise InvalidRequest(
                "GitHub release manifest does not match the TestOps schema"
            ) from exc
        manifest_repository = parse_github_repository(manifest.source.repository_url)
        if manifest_repository.canonical_url.casefold() != repository.canonical_url.casefold():
            raise InvalidRequest(
                "release manifest repository does not match the requested repository"
            )
        if manifest.source.tag != request.tag:
            raise InvalidRequest("release manifest tag does not match the requested GitHub tag")
        if manifest.source.revision != revision:
            raise InvalidRequest(
                "release manifest revision does not match the immutable GitHub tag"
            )
        return FetchedGitRelease(
            repository_url=repository.canonical_url,
            tag=request.tag,
            revision=revision,
            manifest=manifest,
        )
