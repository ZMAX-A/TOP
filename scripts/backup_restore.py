"""Create, verify and explicitly restore TestOps PostgreSQL/MinIO backups."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from hmac import compare_digest
from pathlib import Path, PurePosixPath
from typing import Any

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from sqlalchemy.engine import make_url

BACKUP_FORMAT_VERSION = 1
DATABASE_DUMP_FILE = "postgres.dump"
MANIFEST_FILE = "manifest.json"
MANIFEST_CHECKSUM_FILE = "manifest.sha256"
INCOMPLETE_MARKER = ".incomplete"
CHUNK_SIZE = 1024 * 1024


class BackupError(RuntimeError):
    """Backup or restore preconditions were not satisfied."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _postgres_environment(database_url: str) -> tuple[dict[str, str], str]:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql" or not url.database:
        raise BackupError("DATABASE_URL must point to a PostgreSQL database")
    environment = os.environ.copy()
    mappings: tuple[tuple[str, object | None], ...] = (
        ("PGHOST", url.host),
        ("PGPORT", url.port),
        ("PGDATABASE", url.database),
        ("PGUSER", url.username),
        ("PGPASSWORD", url.password),
        ("PGSSLMODE", url.query.get("sslmode")),
        ("PGSSLROOTCERT", url.query.get("sslrootcert")),
    )
    for name, value in mappings:
        if value is not None:
            environment[name] = str(value)
    return environment, url.database


def _database_context() -> tuple[dict[str, str], str]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise BackupError("DATABASE_URL must be provided through the environment")
    return _postgres_environment(database_url)


def _run(
    command: Sequence[str],
    *,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            tuple(command),
            env=environment,
            check=True,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise BackupError(f"required executable was not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "command failed").strip()
        raise BackupError(f"{command[0]} failed: {detail}") from exc


def _object_store_client() -> Any:
    endpoint = os.getenv("MINIO_ENDPOINT")
    access_key = os.getenv("MINIO_ACCESS_KEY") or os.getenv("MINIO_ROOT_USER")
    secret_key = os.getenv("MINIO_SECRET_KEY") or os.getenv("MINIO_ROOT_PASSWORD")
    if not endpoint or not access_key or not secret_key:
        raise BackupError(
            "MINIO_ENDPOINT, MINIO_ACCESS_KEY and MINIO_SECRET_KEY are required "
            "unless --skip-objects is used"
        )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=os.getenv("MINIO_REGION", "us-east-1"),
        config=Config(signature_version="s3v4"),
    )


def _safe_backup_path(root: Path, relative: str) -> Path:
    if not relative or "\\" in relative:
        raise BackupError(f"invalid backup path: {relative!r}")
    posix_path = PurePosixPath(relative)
    if posix_path.is_absolute() or ".." in posix_path.parts:
        raise BackupError(f"backup path escapes its root: {relative!r}")
    resolved_root = root.resolve()
    resolved = resolved_root.joinpath(*posix_path.parts).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise BackupError(f"backup path escapes its root: {relative!r}")
    return resolved


def _write_manifest(root: Path, manifest: dict[str, Any]) -> None:
    manifest_path = root / MANIFEST_FILE
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    checksum = _sha256(manifest_path).removeprefix("sha256:")
    (root / MANIFEST_CHECKSUM_FILE).write_text(
        f"{checksum}  {MANIFEST_FILE}\n",
        encoding="ascii",
    )


def _load_manifest(root: Path) -> dict[str, Any]:
    if (root / INCOMPLETE_MARKER).exists():
        raise BackupError("backup is marked incomplete")
    manifest_path = root / MANIFEST_FILE
    checksum_path = root / MANIFEST_CHECKSUM_FILE
    if not manifest_path.is_file() or not checksum_path.is_file():
        raise BackupError("backup manifest or checksum is missing")
    checksum_fields = checksum_path.read_text(encoding="ascii").strip().split()
    if len(checksum_fields) != 2 or checksum_fields[1] != MANIFEST_FILE:
        raise BackupError("manifest checksum file is malformed")
    actual = _sha256(manifest_path).removeprefix("sha256:")
    if not compare_digest(checksum_fields[0], actual):
        raise BackupError("manifest checksum does not match")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BackupError("manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        raise BackupError("unsupported backup format version")
    return manifest


def verify_backup(root: Path) -> dict[str, Any]:
    root = root.resolve()
    manifest = _load_manifest(root)
    database = manifest.get("database")
    object_store = manifest.get("object_store")
    if not isinstance(database, dict) or not isinstance(object_store, dict):
        raise BackupError("manifest database or object_store section is invalid")

    database_file = _safe_backup_path(root, str(database.get("file", "")))
    if not database_file.is_file():
        raise BackupError("PostgreSQL dump is missing")
    if database_file.stat().st_size != database.get("size"):
        raise BackupError("PostgreSQL dump size does not match manifest")
    if _sha256(database_file) != database.get("sha256"):
        raise BackupError("PostgreSQL dump checksum does not match manifest")

    entries = object_store.get("objects", [])
    if not isinstance(entries, list):
        raise BackupError("object_store.objects must be a list")
    seen_keys: set[str] = set()
    seen_files: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise BackupError("object manifest entry must be an object")
        key = str(entry.get("key", ""))
        relative = str(entry.get("file", ""))
        if not key or key in seen_keys or relative in seen_files:
            raise BackupError("object manifest contains an empty or duplicate key/file")
        seen_keys.add(key)
        seen_files.add(relative)
        object_file = _safe_backup_path(root, relative)
        if not object_file.is_file():
            raise BackupError(f"object backup is missing: {key}")
        if object_file.stat().st_size != entry.get("size"):
            raise BackupError(f"object size does not match manifest: {key}")
        if _sha256(object_file) != entry.get("sha256"):
            raise BackupError(f"object checksum does not match manifest: {key}")

    return {
        "status": "passed",
        "backup": str(root),
        "database": database.get("name"),
        "object_count": len(entries),
    }


def _download_objects(root: Path, *, bucket: str, client: Any) -> list[dict[str, Any]]:
    object_root = root / "objects"
    object_root.mkdir()
    listed: list[dict[str, Any]] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        listed.extend(page.get("Contents", ()))

    entries: list[dict[str, Any]] = []
    for index, item in enumerate(sorted(listed, key=lambda value: value["Key"]), start=1):
        key = str(item["Key"])
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        relative = f"objects/{index:08d}-{key_digest}.bin"
        destination = _safe_backup_path(root, relative)
        response = client.get_object(Bucket=bucket, Key=key)
        source_metadata = {
            name: str(response.get("Metadata", {}).get(name))
            for name in ("run-id", "artifact-id")
            if response.get("Metadata", {}).get(name)
        }
        body = response["Body"]
        try:
            with destination.open("wb") as output:
                while chunk := body.read(CHUNK_SIZE):
                    output.write(chunk)
        finally:
            body.close()
        actual_size = destination.stat().st_size
        if actual_size != int(item["Size"]):
            raise BackupError(f"object changed while being backed up: {key}")
        entries.append(
            {
                "key": key,
                "file": relative,
                "size": actual_size,
                "sha256": _sha256(destination),
                "etag": str(item.get("ETag", "")).strip('"'),
                "content_type": str(response.get("ContentType", "application/octet-stream")),
                "metadata": source_metadata,
            }
        )
    return entries


def create_backup(
    output_directory: Path,
    *,
    pg_dump: str,
    bucket: str,
    include_objects: bool,
) -> dict[str, Any]:
    root = output_directory.resolve()
    try:
        root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise BackupError(f"backup destination already exists: {root}") from exc
    (root / INCOMPLETE_MARKER).write_text("backup in progress\n", encoding="ascii")

    postgres_environment, database_name = _database_context()
    dump_path = root / DATABASE_DUMP_FILE
    version = _run((pg_dump, "--version"), environment=postgres_environment).stdout.strip()
    _run(
        (
            pg_dump,
            "--format=custom",
            "--no-owner",
            "--no-acl",
            f"--file={dump_path}",
        ),
        environment=postgres_environment,
    )
    if not dump_path.is_file() or not dump_path.stat().st_size:
        raise BackupError("pg_dump did not create a non-empty dump")

    objects: list[dict[str, Any]] = []
    if include_objects:
        objects = _download_objects(root, bucket=bucket, client=_object_store_client())

    manifest = {
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "database": {
            "name": database_name,
            "file": DATABASE_DUMP_FILE,
            "size": dump_path.stat().st_size,
            "sha256": _sha256(dump_path),
            "pg_dump_version": version,
        },
        "object_store": {
            "included": include_objects,
            "bucket": bucket if include_objects else None,
            "objects": objects,
        },
    }
    _write_manifest(root, manifest)
    (root / INCOMPLETE_MARKER).unlink()
    return verify_backup(root)


def _missing_object(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    return code in {"404", "NoSuchKey", "NotFound"}


def _plan_object_restore(
    client: Any,
    *,
    bucket: str,
    entries: list[dict[str, Any]],
    overwrite: bool,
) -> list[dict[str, Any]]:
    uploads: list[dict[str, Any]] = []
    for entry in entries:
        try:
            current = client.head_object(Bucket=bucket, Key=entry["key"])
        except ClientError as exc:
            if _missing_object(exc):
                uploads.append(entry)
                continue
            raise BackupError(f"failed to inspect object: {entry['key']}") from exc
        metadata = {
            str(key).lower(): str(value) for key, value in current.get("Metadata", {}).items()
        }
        same = int(current.get("ContentLength", -1)) == entry["size"] and metadata.get(
            "sha256"
        ) == str(entry["sha256"]).removeprefix("sha256:")
        if same:
            continue
        if not overwrite:
            raise BackupError(
                f"object already exists with different or unverifiable content: {entry['key']}"
            )
        uploads.append(entry)
    return uploads


def restore_backup(
    root: Path,
    *,
    pg_restore: str,
    confirm_database: str,
    replace_database: bool,
    restore_objects: bool,
    overwrite_objects: bool,
) -> dict[str, Any]:
    verification = verify_backup(root)
    manifest = _load_manifest(root.resolve())
    postgres_environment, database_name = _database_context()
    if not replace_database or confirm_database != database_name:
        raise BackupError(
            "restore requires --replace-database and --confirm-database matching DATABASE_URL"
        )

    object_store = manifest["object_store"]
    entries = list(object_store.get("objects", ()))
    uploads: list[dict[str, Any]] = []
    client = None
    if restore_objects and object_store.get("included"):
        client = _object_store_client()
        uploads = _plan_object_restore(
            client,
            bucket=str(object_store["bucket"]),
            entries=entries,
            overwrite=overwrite_objects,
        )

    if client is not None:
        for entry in uploads:
            source = _safe_backup_path(root.resolve(), entry["file"])
            metadata = {
                "sha256": str(entry["sha256"]).removeprefix("sha256:"),
                **{
                    name: str(value)
                    for name, value in entry.get("metadata", {}).items()
                    if name in {"run-id", "artifact-id"}
                },
            }
            extra_args: dict[str, Any] = {"Metadata": metadata}
            if entry.get("content_type"):
                extra_args["ContentType"] = str(entry["content_type"])
            client.upload_file(
                str(source),
                str(object_store["bucket"]),
                entry["key"],
                ExtraArgs=extra_args,
            )

    database_dump = _safe_backup_path(root.resolve(), manifest["database"]["file"])
    _run(
        (
            pg_restore,
            "--clean",
            "--if-exists",
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            f"--dbname={database_name}",
            str(database_dump),
        ),
        environment=postgres_environment,
    )
    return {
        **verification,
        "status": "restored",
        "objects_uploaded": len(uploads),
        "objects_skipped": len(entries) - len(uploads) if restore_objects else len(entries),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup", help="create a new immutable backup directory")
    backup.add_argument("--output-dir", type=Path, required=True)
    backup.add_argument("--pg-dump", default="pg_dump")
    backup.add_argument("--bucket", default=os.getenv("MINIO_BUCKET", "testops-artifacts"))
    backup.add_argument("--skip-objects", action="store_true")

    verify = subparsers.add_parser("verify", help="verify a backup without external writes")
    verify.add_argument("--backup-dir", type=Path, required=True)

    restore = subparsers.add_parser(
        "restore",
        help="restore after explicit destructive confirmation",
    )
    restore.add_argument("--backup-dir", type=Path, required=True)
    restore.add_argument("--pg-restore", default="pg_restore")
    restore.add_argument("--confirm-database", required=True)
    restore.add_argument("--replace-database", action="store_true")
    restore.add_argument("--skip-objects", action="store_true")
    restore.add_argument("--overwrite-objects", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "backup":
            report = create_backup(
                arguments.output_dir,
                pg_dump=arguments.pg_dump,
                bucket=arguments.bucket,
                include_objects=not arguments.skip_objects,
            )
        elif arguments.command == "verify":
            report = verify_backup(arguments.backup_dir)
        else:
            report = restore_backup(
                arguments.backup_dir,
                pg_restore=arguments.pg_restore,
                confirm_database=arguments.confirm_database,
                replace_database=arguments.replace_database,
                restore_objects=not arguments.skip_objects,
                overwrite_objects=arguments.overwrite_objects,
            )
    except BackupError as exc:
        print(json.dumps({"status": "failed", "detail": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
