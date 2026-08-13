"""Verified upload of local Runner artifacts to immutable MinIO object keys."""

from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import UUID

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

from testops.contracts import Artifact, RunResult

from .config import WorkerSettings


class ArtifactUploadError(RuntimeError):
    """A local artifact cannot be verified or durably uploaded."""


def _error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _safe_object_name(name: str) -> str:
    leaf = Path(name).name
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", leaf).strip("._")
    return safe or "artifact"


class ArtifactUploader:
    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str,
        client: BaseClient | None = None,
    ) -> None:
        self.bucket = bucket
        self.region = region
        self._bucket_ready = False
        self._client = client or boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    @classmethod
    def from_settings(cls, settings: WorkerSettings) -> ArtifactUploader:
        if not (
            settings.minio_endpoint
            and settings.minio_access_key
            and settings.minio_secret_key
            and settings.minio_bucket
        ):
            raise ArtifactUploadError("MinIO settings are required to publish Runner artifacts")
        return cls(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            bucket=settings.minio_bucket,
            region=settings.minio_region,
        )

    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            if _error_code(exc) not in {"404", "NoSuchBucket", "NotFound"}:
                raise ArtifactUploadError("unable to inspect the artifact bucket") from exc
            arguments: dict[str, object] = {"Bucket": self.bucket}
            if self.region != "us-east-1":
                arguments["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
            try:
                self._client.create_bucket(**arguments)
            except ClientError as create_error:
                if _error_code(create_error) not in {
                    "BucketAlreadyExists",
                    "BucketAlreadyOwnedByYou",
                }:
                    raise ArtifactUploadError(
                        "unable to create the artifact bucket"
                    ) from create_error
        self._bucket_ready = True

    @staticmethod
    def _local_path(workspace_root: str, run_id: UUID, artifact: Artifact) -> Path:
        parsed = urlsplit(artifact.uri)
        if parsed.scheme != "workspace" or parsed.netloc != str(run_id):
            raise ArtifactUploadError("Runner artifact URI is not in this Run workspace")
        relative = unquote(parsed.path.lstrip("/"))
        parts = relative.split("/")
        if (
            not relative
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in parts)
            or any(ord(character) < 32 for character in relative)
        ):
            raise ArtifactUploadError("Runner artifact path is unsafe")
        run_root = (Path(workspace_root).resolve() / str(run_id)).resolve()
        path = (run_root / Path(*parts)).resolve()
        if run_root not in path.parents or not path.is_file():
            raise ArtifactUploadError("Runner artifact file is missing or escaped its workspace")
        return path

    @staticmethod
    def _verify_local_file(path: Path, artifact: Artifact) -> str:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        actual_digest = f"sha256:{digest.hexdigest()}"
        if size != artifact.size_bytes or actual_digest != artifact.digest:
            raise ArtifactUploadError("Runner artifact metadata does not match its local file")
        return digest.hexdigest()

    def _object_matches(self, key: str, artifact: Artifact, sha256_hex: str) -> bool:
        try:
            response = self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise ArtifactUploadError("unable to inspect an existing artifact object") from exc
        metadata = response.get("Metadata", {})
        if (
            int(response.get("ContentLength", -1)) != artifact.size_bytes
            or metadata.get("sha256") != sha256_hex
        ):
            raise ArtifactUploadError("immutable artifact object conflicts with local content")
        return True

    def upload_result(self, result: RunResult, *, workspace_root: str) -> RunResult:
        if not result.artifacts:
            return result
        self._ensure_bucket()
        uploaded: list[Artifact] = []
        for artifact in result.artifacts:
            path = self._local_path(workspace_root, result.run_id, artifact)
            sha256_hex = self._verify_local_file(path, artifact)
            key = f"runs/{result.run_id}/{artifact.artifact_id}/{_safe_object_name(artifact.name)}"
            if not self._object_matches(key, artifact, sha256_hex):
                content_type = mimetypes.guess_type(artifact.name)[0] or "application/octet-stream"
                try:
                    with path.open("rb") as stream:
                        self._client.put_object(
                            Bucket=self.bucket,
                            Key=key,
                            Body=stream,
                            ContentLength=artifact.size_bytes,
                            ContentType=content_type,
                            Metadata={
                                "sha256": sha256_hex,
                                "run-id": str(result.run_id),
                                "artifact-id": str(artifact.artifact_id),
                            },
                            IfNoneMatch="*",
                        )
                except ClientError as exc:
                    if _error_code(exc) not in {"412", "PreconditionFailed"}:
                        raise ArtifactUploadError("artifact upload failed") from exc
                if not self._object_matches(key, artifact, sha256_hex):
                    raise ArtifactUploadError("uploaded artifact failed integrity verification")
            uploaded.append(artifact.model_copy(update={"uri": f"s3://{self.bucket}/{key}"}))
        return result.model_copy(update={"artifacts": tuple(uploaded)})
