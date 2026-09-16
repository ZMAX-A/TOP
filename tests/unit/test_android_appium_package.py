from __future__ import annotations

from pathlib import Path

from testops.runners.android_appium.package_digest import export_package, package_source_digest


def _create_source(root: Path) -> None:
    for directory in ("config", "scripts", "src/package", "tests"):
        (root / directory).mkdir(parents=True)
    for relative, payload in (
        ("conftest.py", b"conftest"),
        ("pyproject.toml", b"project"),
        ("test_case.xlsx", b"workbook"),
        ("testops-package.json", b"{}"),
        ("config/allure-categories.json", b"{}"),
        ("scripts/run-excel.ps1", b"runner"),
        ("src/package/module.py", b"module"),
        ("tests/test_cases.py", b"tests"),
    ):
        (root / relative).write_bytes(payload)
    egg_info = root / "src/package.egg-info"
    egg_info.mkdir()
    (egg_info / "PKG-INFO").write_text("generated metadata", encoding="utf-8")


def _create_adapter(root: Path) -> None:
    package = root / "testops/runners/android_appium"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "adapter.py").write_text("ADAPTER = True\n", encoding="utf-8")


def test_exported_android_package_recomputes_to_the_same_digest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    adapter = tmp_path / "adapter"
    export = tmp_path / "export"
    source.mkdir()
    adapter.mkdir()
    _create_source(source)
    _create_adapter(adapter)

    source_digest = package_source_digest(source, adapter_root=adapter)
    exported_digest = export_package(source, export, adapter_root=adapter)

    assert exported_digest == source_digest
    assert package_source_digest(export, adapter_root=adapter) == source_digest
    assert not (export / "src/package.egg-info").exists()
