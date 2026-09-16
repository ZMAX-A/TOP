"""Build a deterministic, secret-free digest for the YanJia Android package."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

ANDROID_DIRECTORIES = ("config", "scripts", "src", "tests")
ANDROID_FILES = ("conftest.py", "pyproject.toml", "test_case.xlsx", "testops-package.json")
IGNORED_PARTS = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tmp",
        "outputs",
        "reports",
        ".git",
        ".venv",
    }
)
IGNORED_SUFFIXES = frozenset({".pyc", ".pyo"})


class AndroidPackageExportError(RuntimeError):
    """The Android source cannot be exported through the strict allowlist."""


def package_files(source: str | Path) -> tuple[Path, ...]:
    source_path = Path(source).resolve()
    selected: list[Path] = []
    for relative in ANDROID_FILES:
        path = source_path / relative
        if not path.is_file() or path.is_symlink():
            raise AndroidPackageExportError(f"required regular file is unavailable: {relative}")
        selected.append(path)
    for directory in ANDROID_DIRECTORIES:
        root = source_path / directory
        if not root.is_dir() or root.is_symlink():
            raise AndroidPackageExportError(f"required directory is unavailable: {directory}")
        for path in root.rglob("*"):
            relative = path.relative_to(source_path)
            if any(part in IGNORED_PARTS for part in relative.parts):
                continue
            if path.is_symlink():
                raise AndroidPackageExportError(
                    f"package source cannot contain symlinks: {relative.as_posix()}"
                )
            if path.is_file() and path.suffix.lower() not in IGNORED_SUFFIXES:
                selected.append(path)
    return tuple(sorted(set(selected), key=lambda path: path.relative_to(source_path).as_posix()))


def package_source_digest(source: str | Path, *, adapter_root: str | Path | None = None) -> str:
    source_path = Path(source).resolve()
    adapter_path = (
        Path(adapter_root).resolve()
        if adapter_root is not None
        else Path(__file__).resolve().parents[4]
    )
    documents: list[tuple[str, Path]] = [
        (f"android/{path.relative_to(source_path).as_posix()}", path)
        for path in package_files(source_path)
    ]
    documents.extend(
        (f"adapter/{path.relative_to(adapter_path).as_posix()}", path)
        for path in adapter_path.rglob("*.py")
        if path.is_file()
    )
    digest = hashlib.sha256()
    for relative, path in sorted(documents):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def export_package(source: str | Path, destination: str | Path) -> str:
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if destination_path == source_path or source_path in destination_path.parents:
        raise AndroidPackageExportError("export destination must be outside the source project")
    if destination_path.exists() and any(destination_path.iterdir()):
        raise AndroidPackageExportError("export destination must not exist or must be empty")
    destination_path.mkdir(parents=True, exist_ok=True)
    for path in package_files(source_path):
        relative = path.relative_to(source_path)
        output = destination_path / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, output)
    return package_source_digest(source_path)
