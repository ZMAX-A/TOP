from __future__ import annotations

import tempfile
from pathlib import Path

from scripts.package_legacy_pytest import export_package, package_source_digest

ROOT = Path(__file__).resolve().parents[2]


def create_source(root: Path) -> None:
    for directory in ("pages", "tests", "utils", "test_cases"):
        (root / directory).mkdir()
    for relative, payload in (
        ("pages/login.py", b"login"),
        ("tests/conftest.py", b"fixtures"),
        ("tests/test_core_cases.py", b"cases"),
        ("utils/excel_handler.py", b"excel"),
        ("test_cases/test_case.xlsx", b"workbook"),
        ("pytest.ini", b"[pytest]"),
        ("requirements.txt", b"pytest==9.1.1"),
        ("run_parallel_tests.py", b"parallel"),
    ):
        (root / relative).write_bytes(payload)


def test_package_export_is_allowlisted_and_digest_stable() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "source"
        export = root / "export"
        source.mkdir()
        create_source(source)
        (source / ".env").write_text("TEST_PASSWORD=must-not-export", encoding="utf-8")
        (source / "reports").mkdir()
        (source / "reports/result.json").write_text("{}", encoding="utf-8")

        original_digest = package_source_digest(source)
        exported_digest = export_package(source, export)

        assert exported_digest == original_digest
        assert package_source_digest(export) == original_digest
        assert not (export / ".env").exists()
        assert not (export / "reports").exists()


def test_central_pytest_compose_uses_a_dedicated_immutable_package_image() -> None:
    compose = (ROOT / "compose.yanjia-pytest.yaml").read_text("utf-8")
    dockerfile = (ROOT / "infra/docker/yanjia-web-pytest.Dockerfile").read_text("utf-8")

    assert "YANJIA_WEB_PACKAGE_CONTEXT:?" in compose
    assert "YANJIA_PYTEST_PACKAGE_DIGEST:?" in compose
    assert "YANJIA_PYTEST_IMAGE_TAG:-0.35.2" in compose
    assert "TESTOPS_IMAGE_TAG" not in compose
    assert '"image_repository":"testops-yanjia-web-pytest"' in compose
    assert "TESTOPS_SECRET_TEST_USERNAME" in compose
    assert "TESTOPS_SECRET_TEST_PASSWORD" in compose
    assert "--queues=celery,testops.pool.${YANJIA_RUNNER_POOL_KEY:-yanjia-web}" in compose
    assert "      - runner-sandbox" in compose
    assert "  runner-sandbox:\n" in compose
    assert "    internal: false" in compose
    assert "COPY --from=yanjia_web" in dockerfile
    assert "COPY --from=yanjia_web ." not in dockerfile
    assert "test_cases/test_case.xlsx" in dockerfile
    assert "run_parallel_tests.py" in dockerfile
    assert "chmod -R a-w /opt/yanjia-web" in dockerfile
