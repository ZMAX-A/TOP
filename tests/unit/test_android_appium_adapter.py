from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from testops.contracts import (
    AssertionDefinition,
    AutomationPackageRef,
    CaseBaselineRef,
    CaseDefinition,
    CasePriority,
    RunSnapshot,
    RunStatus,
    SecretBinding,
    StepDefinition,
    TargetType,
)
from testops.runners.android_appium import ANDROID_PACKAGE_NAME, AndroidAppiumAdapter
from testops.runners.android_appium.adapter import CommandResult
from testops.runners.android_appium.package_digest import package_source_digest
from testops.runners.web import MappingSecretProvider


class FakeCommandRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(self, run_id, command, *, cwd, environ, timeout_seconds, on_output=None):
        del run_id, cwd, timeout_seconds
        self.commands.append(tuple(command))
        assert environ["YANJIA_IGNORE_DOTENV"] == "true"
        reports_root = Path(command[command.index("-ReportsRoot") + 1])
        result_run_id = command[command.index("-RunId") + 1]
        case_codes = command[command.index("-CaseId") + 1].split(",")
        result_root = reports_root / "allure-results" / result_run_id
        result_root.mkdir(parents=True, exist_ok=True)
        for index, case_code in enumerate(case_codes):
            (result_root / f"{index}-result.json").write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "start": 1_780_000_000_000,
                        "stop": 1_780_000_001_000,
                        "labels": [{"name": "case_id", "value": case_code}],
                    }
                ),
                encoding="utf-8",
            )
            if on_output is not None:
                on_output(f"{case_code} PASSED")
        return CommandResult(returncode=0, output="all passed", duration_seconds=1.0)

    def cancel(self, run_id) -> None:
        del run_id


def _project(root: Path, *, package_version: str = "1.0.7") -> None:
    for directory in ("config", "scripts", "src", "tests", ".venv/Scripts"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for relative in (
        "conftest.py",
        "pyproject.toml",
        "test_case.xlsx",
        ".venv/Scripts/python.exe",
        "scripts/run-excel.ps1",
        "src/package.py",
        "tests/test_cases.py",
        "config/allure-categories.json",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
    (root / "testops-package.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "name": ANDROID_PACKAGE_NAME,
                "version": package_version,
                "runner_type": "ANDROID_APPIUM",
                "entrypoint": "scripts/run-excel.ps1",
                "workbook": "test_case.xlsx",
                "target_type": "APP",
            }
        ),
        encoding="utf-8",
    )


def test_android_adapter_executes_exact_case_without_local_dotenv(tmp_path: Path) -> None:
    project = tmp_path / "app"
    _project(project)
    case = CaseDefinition(
        case_id=uuid4(),
        case_code="TC-HOME-001",
        module_key="home",
        module_name="首页",
        title="首页加载",
        test_point="首页加载",
        priority=CasePriority.P0,
        steps=(StepDefinition(operation="execute_excel_case", input="TC-HOME-001"),),
        assertion=AssertionDefinition(type="excel_case_passed", expected="PASS"),
        tags=("readonly",),
        timeout_seconds=30,
    )
    snapshot = RunSnapshot(
        run_id=uuid4(),
        project_id=uuid4(),
        target_id=uuid4(),
        target_type=TargetType.APP,
        environment_id=uuid4(),
        case_baseline=CaseBaselineRef(
            baseline_id=uuid4(),
            version="case-v1.0.0",
            digest="sha256:" + "1" * 64,
            case_count=1,
        ),
        automation_package=AutomationPackageRef(
            name=ANDROID_PACKAGE_NAME,
            version="1.0.7",
            runner_type="ANDROID_APPIUM",
            image_repository="local/yanjia-android-appium",
            digest=package_source_digest(project),
        ),
        cases=(case,),
        config_hash="sha256:" + "2" * 64,
        secret_bindings=(
            SecretBinding(name="YANJIA_USERNAME", ref="secret://android/username"),
            SecretBinding(name="YANJIA_PASSWORD", ref="secret://android/password"),
        ),
        created_by=uuid4(),
        created_at=datetime.now(UTC),
    )
    commands = FakeCommandRunner()
    adapter = AndroidAppiumAdapter(
        workspace_root=tmp_path / "runs",
        project_root=project,
        secret_provider=MappingSecretProvider(
            {
                "secret://android/username": "runner-user",
                "secret://android/password": "runner-password",
            }
        ),
        command_runner=commands,
        powershell_executable="powershell.exe",
    )

    adapter.prepare(snapshot)
    result = adapter.execute(snapshot, lambda _event: None)

    assert result.status == RunStatus.PASSED
    assert result.case_results[0].case_code == "TC-HOME-001"
    assert result.case_results[0].status.value == "PASSED"
    assert len(commands.commands) == 1
    assert "-IgnoreDotEnv" in commands.commands[0]
    assert "-NoWriteBack" in commands.commands[0]
    assert "-AllowMutation" not in commands.commands[0]
    assert any(artifact.kind.value == "ALLURE" for artifact in result.artifacts)
