"""Short-lived, project-authorized access to immutable object-store artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID

import boto3
from botocore.client import BaseClient
from botocore.config import Config

from .config import Settings


class ArtifactStoreError(RuntimeError):
    """An artifact location or object-store configuration is unsafe."""


@dataclass(frozen=True, slots=True)
class ObjectLocation:
    bucket: str
    key: str


@dataclass(frozen=True, slots=True)
class ArtifactAccess:
    url: str
    expires_in_seconds: int


def parse_artifact_uri(uri: str, *, run_id: UUID, expected_bucket: str) -> ObjectLocation:
    parsed = urlsplit(uri)
    if parsed.scheme != "s3" or parsed.query or parsed.fragment:
        raise ArtifactStoreError("artifact does not reference managed object storage")
    if parsed.netloc != expected_bucket:
        raise ArtifactStoreError("artifact references an unexpected object-storage bucket")
    key = unquote(parsed.path.lstrip("/"))
    parts = key.split("/")
    if (
        not key
        or "\\" in key
        or any(part in {"", ".", ".."} for part in parts)
        or any(ord(character) < 32 for character in key)
    ):
        raise ArtifactStoreError("artifact object key is unsafe")
    if parts[:2] != ["runs", str(run_id)]:
        raise ArtifactStoreError("artifact object key does not belong to this Run")
    return ObjectLocation(bucket=parsed.netloc, key=key)


def _content_disposition(filename: str) -> str:
    safe_name = filename.replace("\r", "").replace("\n", "")
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", safe_name).strip("._") or "artifact"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe_name, safe='')}"


class MinioArtifactStore:
    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str,
        url_ttl_seconds: int,
        client: BaseClient | None = None,
    ) -> None:
        self.bucket = bucket
        self.url_ttl_seconds = url_ttl_seconds
        self._client = client or boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> MinioArtifactStore | None:
        if not (
            settings.minio_endpoint
            and settings.minio_access_key
            and settings.minio_secret_key
            and settings.minio_bucket
        ):
            return None
        return cls(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            bucket=settings.minio_bucket,
            region=settings.minio_region,
            url_ttl_seconds=settings.artifact_url_ttl_seconds,
        )

    def create_download_access(
        self,
        uri: str,
        *,
        run_id: UUID,
        filename: str,
    ) -> ArtifactAccess:
        location = parse_artifact_uri(uri, run_id=run_id, expected_bucket=self.bucket)
        url = self._client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": location.bucket,
                "Key": location.key,
                "ResponseContentDisposition": _content_disposition(filename),
            },
            ExpiresIn=self.url_ttl_seconds,
        )
        return ArtifactAccess(url=url, expires_in_seconds=self.url_ttl_seconds)
