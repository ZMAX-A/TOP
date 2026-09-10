"""Hash or export the allowlisted YanJia Pytest package build context."""

from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY_DIRECTORIES = ("pages", "tests", "utils", "test_assets")
LEGACY_FILES = (
    "pytest.ini",
    "requirements.txt",
    "run_parallel_tests.py",
    "test_cases/test_case.xlsx",
)
IGNORED_PARTS = frozenset({"__pycache__", ".pytest_cache"})


class PackageExportError(RuntimeError):
    """The legacy project cannot be exported without widening the package boundary."""


def _legacy_files(source: Path) -> tuple[Path, ...]:
    selected: list[Path] = []
    for relative in LEGACY_FILES:
        path = source / relative
        if not path.is_file() or path.is_symlink():
            raise PackageExportError(f"required regular file is unavailable: {relative}")
        selected.append(path)
    for directory in LEGACY_DIRECTORIES:
        root = source / directory
        if not root.is_dir() or root.is_symlink():
            raise PackageExportError(f"required directory is unavailable: {directory}")
        for path in root.rglob("*"):
            relative = path.relative_to(source)
            if any(part in IGNORED_PARTS for part in relative.parts):
                continue
            if path.is_symlink():
                raise PackageExportError(f"package source cannot contain symlinks: {relative}")
            if path.is_file() and path.suffix.lower() != ".pyc":
                selected.append(path)
    return tuple(sorted(set(selected), key=lambda path: path.relative_to(source).as_posix()))


def package_source_digest(source: str | Path, *, platform_root: Path = ROOT) -> str:
    source_path = Path(source).resolve()
    digest = hashlib.sha256()
    documents: list[tuple[str, Path]] = [
        (f"legacy/{path.relative_to(source_path).as_posix()}", path)
        for path in _legacy_files(source_path)
    ]
    adapter_root = platform_root / "runners/legacy_pytest/src"
    documents.extend(
        (f"adapter/{path.relative_to(adapter_root).as_posix()}", path)
        for path in adapter_root.rglob("*.py")
        if path.is_file()
    )
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
        raise PackageExportError("export destination must be outside the source project")
    if destination_path.exists() and any(destination_path.iterdir()):
        raise PackageExportError("export destination must not exist or must be empty")
    destination_path.mkdir(parents=True, exist_ok=True)
    for path in _legacy_files(source_path):
        relative = path.relative_to(source_path)
        output = destination_path / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, output)
    return package_source_digest(source_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hash or export the allowlisted YanJia Pytest package"
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--export-to", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    digest = (
        export_package(args.source, args.export_to)
        if args.export_to
        else package_source_digest(args.source)
    )
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
