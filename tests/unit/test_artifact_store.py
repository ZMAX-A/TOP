from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from botocore.exceptions import ClientError

from testops.api.artifact_store import (
    ArtifactStoreError,
    MinioArtifactStore,
    parse_artifact_uri,
)
from testops.contracts import Artifact, ArtifactKind, RunResult, RunStatus
from testops.worker.artifact_store import ArtifactUploader, ArtifactUploadError


def _not_found(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "404", "Message": "not found"}},
        operation,
    )


class FakeS3Client:
    def __init__(self) -> None:
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}
        self.put_count = 0

    def head_bucket(self, *, Bucket: str) -> dict[str, object]:
        if Bucket not in self.buckets:
            raise _not_found("HeadBucket")
        return {}

    def create_bucket(self, *, Bucket: str, **_kwargs: object) -> dict[str, object]:
        self.buckets.add(Bucket)
        return {}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        try:
            content, metadata = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise _not_found("HeadObject") from exc
        return {"ContentLength": len(content), "Metadata": metadata}

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: object,
        Metadata: dict[str, str],
        **_kwargs: object,
    ) -> dict[str, object]:
        content = Body.read()
        self.objects[(Bucket, Key)] = (content, Metadata)
        self.put_count += 1
        return {}

    def generate_presigned_url(
        self,
        operation: str,
        *,
        Params: dict[str, str],
        ExpiresIn: int,
    ) -> str:
        return (
            f"https://objects.example.invalid/{Params['Bucket']}/{Params['Key']}"
            f"?operation={operation}&expires={ExpiresIn}"
        )


class ArtifactStoreTests(unittest.TestCase):
    def test_api_store_rejects_external_or_cross_run_locations(self) -> None:
        run_id = uuid4()
        valid = parse_artifact_uri(
            f"s3://testops-artifacts/runs/{run_id}/artifact/screenshot.png",
            run_id=run_id,
            expected_bucket="testops-artifacts",
        )
        self.assertEqual(valid.key, f"runs/{run_id}/artifact/screenshot.png")
        with self.assertRaises(ArtifactStoreError):
            parse_artifact_uri(
                "https://example.invalid/file.png",
                run_id=run_id,
                expected_bucket="testops-artifacts",
            )
        with self.assertRaises(ArtifactStoreError):
            parse_artifact_uri(
                f"s3://other-bucket/runs/{run_id}/file.png",
                run_id=run_id,
                expected_bucket="testops-artifacts",
            )
        with self.assertRaises(ArtifactStoreError):
            parse_artifact_uri(
                f"s3://testops-artifacts/runs/{uuid4()}/file.png",
                run_id=run_id,
                expected_bucket="testops-artifacts",
            )

    def test_presigned_access_is_short_lived(self) -> None:
        run_id = uuid4()
        fake = FakeS3Client()
        store = MinioArtifactStore(
            endpoint="http://minio:9000",
            access_key="access",
            secret_key="secret",
            bucket="testops-artifacts",
            region="us-east-1",
            url_ttl_seconds=120,
            client=fake,
        )
        access = store.create_download_access(
            f"s3://testops-artifacts/runs/{run_id}/artifact/events.jsonl",
            run_id=run_id,
            filename="事件 日志.jsonl",
        )
        self.assertEqual(access.expires_in_seconds, 120)
        self.assertIn("expires=120", access.url)

    def test_worker_upload_is_verified_immutable_and_idempotent(self) -> None:
        run_id = uuid4()
        artifact_id = uuid4()
        payload = b"artifact-content"
        digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            screenshot = root / str(run_id) / "screenshots" / "failed.png"
            screenshot.parent.mkdir(parents=True)
            screenshot.write_bytes(payload)
            artifact = Artifact(
                artifact_id=artifact_id,
                kind=ArtifactKind.SCREENSHOT,
                name="failed.png",
                uri=f"workspace://{run_id}/screenshots/failed.png",
                digest=digest,
                size_bytes=len(payload),
            )
            moment = datetime.now(UTC)
            result = RunResult(
                run_id=run_id,
                status=RunStatus.PASSED,
                started_at=moment,
                finished_at=moment,
                runner_version="0.4.0",
                case_results=(),
                artifacts=(artifact,),
            )
            fake = FakeS3Client()
            uploader = ArtifactUploader(
                endpoint="http://minio:9000",
                access_key="access",
                secret_key="secret",
                bucket="testops-artifacts",
                region="us-east-1",
                client=fake,
            )

            uploaded = uploader.upload_result(result, workspace_root=temporary_directory)
            self.assertEqual(fake.put_count, 1)
            self.assertEqual(
                uploaded.artifacts[0].uri,
                f"s3://testops-artifacts/runs/{run_id}/{artifact_id}/failed.png",
            )
            replayed = uploader.upload_result(result, workspace_root=temporary_directory)
            self.assertEqual(replayed.artifacts[0].uri, uploaded.artifacts[0].uri)
            self.assertEqual(fake.put_count, 1)

            escaped = result.model_copy(
                update={
                    "artifacts": (
                        artifact.model_copy(update={"uri": f"workspace://{run_id}/../outside.png"}),
                    )
                }
            )
            with self.assertRaises(ArtifactUploadError):
                uploader.upload_result(escaped, workspace_root=temporary_directory)

            screenshot.write_bytes(b"tampered")
            with self.assertRaises(ArtifactUploadError):
                uploader.upload_result(result, workspace_root=temporary_directory)


if __name__ == "__main__":
    unittest.main()
