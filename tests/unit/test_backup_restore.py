from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.backup_restore import (
    BACKUP_FORMAT_VERSION,
    BackupError,
    _plan_object_restore,
    _postgres_environment,
    _sha256,
    _write_manifest,
    verify_backup,
)


def _fixture_backup(root: Path) -> Path:
    root.mkdir()
    dump = root / "postgres.dump"
    dump.write_bytes(b"postgres-custom-dump")
    object_directory = root / "objects"
    object_directory.mkdir()
    object_file = object_directory / "00000001-deadbeef.bin"
    object_file.write_bytes(b"trace artifact")
    _write_manifest(
        root,
        {
            "format_version": BACKUP_FORMAT_VERSION,
            "created_at": "2026-08-12T00:00:00+00:00",
            "database": {
                "name": "testops",
                "file": "postgres.dump",
                "size": dump.stat().st_size,
                "sha256": _sha256(dump),
                "pg_dump_version": "pg_dump 17",
            },
            "object_store": {
                "included": True,
                "bucket": "testops-artifacts",
                "objects": [
                    {
                        "key": "runs/example/trace.zip",
                        "file": "objects/00000001-deadbeef.bin",
                        "size": object_file.stat().st_size,
                        "sha256": _sha256(object_file),
                        "etag": "example",
                    }
                ],
            },
        },
    )
    return root


def test_verify_backup_checks_database_manifest_and_objects(tmp_path: Path) -> None:
    backup = _fixture_backup(tmp_path / "backup")

    report = verify_backup(backup)

    assert report == {
        "status": "passed",
        "backup": str(backup.resolve()),
        "database": "testops",
        "object_count": 1,
    }


def test_verify_backup_rejects_tampering_and_path_escape(tmp_path: Path) -> None:
    backup = _fixture_backup(tmp_path / "tampered")
    (backup / "objects/00000001-deadbeef.bin").write_bytes(b"changed")
    with pytest.raises(BackupError, match="size|checksum"):
        verify_backup(backup)

    escaped = _fixture_backup(tmp_path / "escaped")
    manifest_path = escaped / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["object_store"]["objects"][0]["file"] = "../outside.bin"
    _write_manifest(escaped, manifest)
    with pytest.raises(BackupError, match="escapes"):
        verify_backup(escaped)


def test_postgres_environment_keeps_password_out_of_command_arguments() -> None:
    environment, database = _postgres_environment(
        "postgresql+asyncpg://backup-user:p%40ss@database.internal:5433/restore_test"
        "?sslmode=require"
    )

    assert database == "restore_test"
    assert environment["PGHOST"] == "database.internal"
    assert environment["PGPORT"] == "5433"
    assert environment["PGUSER"] == "backup-user"
    assert environment["PGPASSWORD"] == "p@ss"
    assert environment["PGSSLMODE"] == "require"


def test_object_restore_accepts_worker_sha256_metadata_format() -> None:
    entry = {
        "key": "runs/example/trace.zip",
        "file": "objects/trace.bin",
        "size": 12,
        "sha256": "sha256:" + "a" * 64,
    }

    class ExistingObjectClient:
        @staticmethod
        def head_object(*, Bucket: str, Key: str) -> dict[str, object]:
            assert Bucket == "testops-artifacts"
            assert Key == entry["key"]
            return {"ContentLength": 12, "Metadata": {"sha256": "a" * 64}}

    assert (
        _plan_object_restore(
            ExistingObjectClient(),
            bucket="testops-artifacts",
            entries=[entry],
            overwrite=False,
        )
        == []
    )
