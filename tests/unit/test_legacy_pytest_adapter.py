from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from openpyxl import Workbook

from testops.contracts import ArtifactKind, RunSnapshot, RunStatus, RuntimeVariable, SecretBinding
from testops.runners.legacy_pytest import LEGACY_PACKAGE_NAME, LegacyPytestAdapter
from testops.runners.legacy_pytest.adapter import (
    CommandResult,
    LegacyProgressTracker,
    LegacyPytestValidationError,
    SubprocessCommandRunner,
)
from testops.runners.web import MappingSecretProvider, PlaywrightWebAdapter
from testops.worker.adapter_factory import create_automation_adapter

ROOT = Path(__file__).resolve().parents[2]


class FakeCommandRunner:
    def __init__(self, *, write_result: bool = True) -> None:
        self.write_result = write_result
        self.command: tuple[str, ...] = ()
        self.environment: dict[str, str] = {}
        self.cwd: Path | None = None
        self.canceled = False

    def run(self, run_id, command, *, cwd, environ, timeout_seconds, on_output=None):
        self.command = tuple(command)
        self.environment = dict(environ)
        self.cwd = cwd
        runtime_home = Path(environ["HOME"])
        (runtime_home / "browser.cache").write_text("ephemeral browser state", encoding="utf-8")
        runtime_temp = Path(environ["TMPDIR"])
        (runtime_temp / "browser.tmp").write_text("ephemeral browser temp", encoding="utf-8")
        if self.write_result:
            if on_output is not None:
                on_output(
                    "[B] tests/test_core_cases.py::TestCoreCases::"
                    "test_execute[[TC-LOGIN-001] 登录] PASSED [100%]"
                )
            result_path = cwd / "reports" / "runs" / "RUN-test" / "summary.json"
            result_path.parent.mkdir(parents=True)
            result_path.write_text(
                json.dumps(
                    {
                        "run_id": "RUN-test",
                        "mode": "parallel-two-account",
                        "group_counts": {"A": 0, "B": 1, "SERIAL": 0},
                        "worker_exit_codes": {"A": 5, "B": 0},
                        "result_counts": {
                            "pass": 1,
                            "fail": 0,
                            "skip": 0,
                            "infra_error": 0,
                        },
                        "results": [
                            {
                                "case_id": "TC-LOGIN-001",
                                "status": "pass",
                                "result": "pass",
                                "row": 2,
                                "duration": 0.125,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            allure_dir = result_path.parent / "worker_B" / "allure-results"
            allure_dir.mkdir(parents=True)
            (allure_dir / "case-result.json").write_text("{}", encoding="utf-8")
            (allure_dir.parent / "auth_state.json").write_text("session-cookie", encoding="utf-8")
            (cwd / "test_cases" / "test_case.xlsx").write_bytes(b"runtime-result-copy")
        return CommandResult(
            returncode=0,
            output="password=bound-password-b\n",
            duration_seconds=0.2,
        )

    def cancel(self, run_id):
        self.canceled = True


def legacy_job() -> RunSnapshot:
    job = RunSnapshot.model_validate_json(
        (ROOT / "tests/fixtures/run_snapshot.valid.json").read_text("utf-8")
    )
    return job.model_copy(
        update={
            "automation_package": job.automation_package.model_copy(
                update={"name": LEGACY_PACKAGE_NAME}
            ),
            "secret_bindings": tuple(
                SecretBinding(name=name, ref=f"secret://yanjia/staging/{name.lower()}")
                for name in (
                    "TEST_USERNAME_A",
                    "TEST_PASSWORD_A",
                    "TEST_USERNAME_B",
                    "TEST_PASSWORD_B",
                )
            ),
        }
    )


def create_project(root: Path) -> None:
    for directory in ("pages", "tests", "utils", "test_assets", "test_cases"):
        (root / directory).mkdir()
    for relative in (
        "tests/conftest.py",
        "tests/test_core_cases.py",
        "pytest.ini",
        "run_parallel_tests.py",
    ):
        (root / relative).write_bytes(b"placeholder")
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "自动化测试用例"
    worksheet.append(
        [
            "用例ID",
            "模块",
            "测试场景",
            "测试点",
            "优先级",
            "前置条件",
            "操作步骤",
            "元素定位器",
            "操作类型",
            "输入数据",
            "数据类型",
            "期望结果",
            "验证点",
            "断言类型",
            "超时(秒)",
            "备注",
            "实际结果",
            "是否执行",
            "执行分组",
        ]
    )
    worksheet.append(
        [
            "TC-LOGIN-001",
            "旧模块",
            "旧场景",
            "旧测试点",
            "P3",
            "",
            "",
            "#stale",
            "click",
            "",
            "",
            "旧预期",
            "旧验证点",
            "text_visible",
            5,
            "",
            "",
            "是",
            "B",
        ]
    )
    workbook.save(root / "test_cases/test_case.xlsx")
    workbook.close()


def test_legacy_adapter_runs_exact_cases_with_minimized_environment() -> None:
    job = legacy_job()
    command_runner = FakeCommandRunner()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        project_root = root / "project"
        workspace_root = root / "runs"
        project_root.mkdir()
        create_project(project_root)
        source_workbook_before = (project_root / "test_cases/test_case.xlsx").read_bytes()
        adapter = LegacyPytestAdapter(
            workspace_root=workspace_root,
            project_root=project_root,
            secret_provider=MappingSecretProvider(
                {
                    "secret://yanjia/staging/test_username_a": "bound-user-a",
                    "secret://yanjia/staging/test_password_a": "bound-password-a",
                    "secret://yanjia/staging/test_username_b": "bound-user-b",
                    "secret://yanjia/staging/test_password_b": "bound-password-b",
                }
            ),
            command_runner=command_runner,
            parent_environment={
                "PATH": "safe-path",
                "DISPLAY": ":99",
                "XAUTHORITY": "/tmp/testops-xauthority",
                "DATABASE_URL": "must-not-cross",
            },
        )
        events: list[dict[str, object]] = []

        result = adapter.execute(job, events.append)

        log = (workspace_root / str(job.run_id) / "parallel-runner.log").read_text("utf-8")
        runtime_home = workspace_root / str(job.run_id) / "home"
        runtime_temp = Path(command_runner.environment["TMPDIR"])
        source_workbook_after = (project_root / "test_cases/test_case.xlsx").read_bytes()

    assert result.status == RunStatus.PASSED
    assert result.case_results[0].case_code == "TC-LOGIN-001"
    assert result.case_results[0].duration_ms == 125
    assert command_runner.cwd is not None
    assert command_runner.cwd.name == "_runtime-project"
    assert command_runner.cwd != project_root
    assert command_runner.environment["BASE_URL"] == job.web_config.base_url
    assert command_runner.environment["TEST_USERNAME_A"] == "bound-user-a"
    assert command_runner.environment["TEST_PASSWORD_A"] == "bound-password-a"
    assert command_runner.environment["TEST_USERNAME_B"] == "bound-user-b"
    assert command_runner.environment["TEST_PASSWORD_B"] == "bound-password-b"
    assert command_runner.environment["DISPLAY"] == ":99"
    assert command_runner.environment["XAUTHORITY"] == "/tmp/testops-xauthority"
    assert command_runner.environment["TEMP"] == str(runtime_temp)
    assert command_runner.environment["TMP"] == str(runtime_temp)
    assert runtime_temp.name.startswith(f"testops-{job.run_id.hex[:8]}-")
    assert "DATABASE_URL" not in command_runner.environment
    assert command_runner.command[:5] == (
        command_runner.command[0],
        "run_parallel_tests.py",
        "--skip-checks",
        "--no-report",
        "--no-open",
    )
    assert "--headed" not in command_runner.command
    assert command_runner.command[command_runner.command.index("-k") + 1] == "TC-LOGIN-001"
    assert "bound-password-b" not in log
    assert "***" in log
    assert not runtime_home.exists()
    assert not runtime_temp.exists()
    assert "browser.cache" not in {artifact.name for artifact in result.artifacts}
    assert source_workbook_after == source_workbook_before
    assert ArtifactKind.ALLURE in {artifact.kind for artifact in result.artifacts}
    assert "auth_state.json" not in {artifact.name for artifact in result.artifacts}
    assert [event["event"] for event in events] == [
        "run_started",
        "progress_updated",
        "case_started",
        "progress_updated",
        "case_finished",
        "run_finished",
    ]
    assert events[3]["completed"] == 1
    assert events[3]["passed"] == 1
    assert events[3]["failed"] == 0


def test_legacy_adapter_timeout_covers_two_delayed_rerun_rounds() -> None:
    short_case = legacy_job().cases[0].model_copy(update={"timeout_seconds": 3})

    assert LegacyPytestAdapter._execution_timeout((short_case,)) == 159


def test_legacy_adapter_timeout_scales_orchestration_grace_with_case_count() -> None:
    short_case = legacy_job().cases[0].model_copy(update={"timeout_seconds": 3})

    assert LegacyPytestAdapter._execution_timeout((short_case,) * 93) == 3747


def test_legacy_progress_tracks_initial_and_two_failed_case_rerun_rounds() -> None:
    run_id = legacy_job().run_id
    moment = legacy_job().created_at
    tracker = LegacyProgressTracker(("TC-HOME-001", "TC-HOME-002", "TC-HOME-003"))

    initial = tracker.initial_event(run_id, moment)
    passed = tracker.consume(
        run_id,
        "[A] tests/test_core_cases.py::test_execute[[TC-HOME-001] 首页] PASSED [33%]",
        moment,
    )
    failed = tracker.consume(
        run_id,
        "\x1b[31m[B] tests/test_core_cases.py::test_execute"
        "[[TC-HOME-002] 首页] FAILED [67%]\x1b[0m",
        moment,
    )
    skipped = tracker.consume(
        run_id,
        "[A] tests/test_core_cases.py::test_execute[[TC-HOME-003] 首页] SKIPPED [100%]",
        moment,
    )
    second_round = tracker.consume(
        run_id,
        "第 1 轮复跑：2 条失败用例，等待 30 秒后逐条复跑...",
        moment,
    )
    recovered = tracker.consume(
        run_id,
        "  [TC-HOME-002] 第 1 轮复跑通过 → 最终判定 pass",
        moment,
    )
    third_round = tracker.consume(
        run_id,
        "第 2 轮复跑：1 条失败用例，等待 30 秒后逐条复跑...",
        moment,
    )
    still_failed = tracker.consume(
        run_id,
        "  [TC-HOME-003] 第 2 轮复跑仍失败",
        moment,
    )

    assert initial["current_round"] == 1
    assert passed is not None and passed["passed"] == 1
    assert failed is not None and failed["failed"] == 1
    assert skipped is not None and skipped["skipped"] == 1
    assert second_round is not None and second_round["current_round"] == 2
    assert second_round["round_total"] == 2
    assert recovered is not None and recovered["passed"] == 2
    assert recovered["failed"] == 0
    assert third_round is not None and third_round["current_round"] == 3
    assert still_failed is not None and still_failed["failed"] == 1
    assert still_failed["round_completed"] == 1


def test_legacy_progress_ignores_duplicate_pytest_summary_lines() -> None:
    job = legacy_job()
    tracker = LegacyProgressTracker(("TC-LOGIN-001",))
    line = "tests/test_core_cases.py::test_execute[[TC-LOGIN-001] 登录] PASSED [100%]"

    first = tracker.consume(job.run_id, line, job.created_at)
    duplicate = tracker.consume(job.run_id, f"PASSED {line}", job.created_at)

    assert first is not None and first["round_completed"] == 1
    assert duplicate is None


def test_subprocess_command_runner_streams_output_and_retains_complete_log() -> None:
    job = legacy_job()
    observed: list[str] = []
    with tempfile.TemporaryDirectory() as temporary:
        result = SubprocessCommandRunner().run(
            job.run_id,
            (
                sys.executable,
                "-c",
                "print('first', flush=True); print('second', flush=True)",
            ),
            cwd=Path(temporary),
            environ=os.environ,
            timeout_seconds=10,
            on_output=observed.append,
        )

    assert result.returncode == 0
    assert observed == ["first", "second"]
    assert result.output == "first\nsecond\n"


def test_legacy_adapter_maps_missing_result_to_infrastructure_error() -> None:
    job = legacy_job()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        project_root = root / "project"
        project_root.mkdir()
        create_project(project_root)
        adapter = LegacyPytestAdapter(
            workspace_root=root / "runs",
            project_root=project_root,
            secret_provider=MappingSecretProvider(
                {
                    "secret://yanjia/staging/test_username_a": "bound-user-a",
                    "secret://yanjia/staging/test_password_a": "bound-password-a",
                    "secret://yanjia/staging/test_username_b": "bound-user-b",
                    "secret://yanjia/staging/test_password_b": "bound-password-b",
                }
            ),
            command_runner=FakeCommandRunner(write_result=False),
            parent_environment={},
        )

        result = adapter.execute(job, lambda _event: None)

    assert result.status == RunStatus.INFRA_ERROR
    assert result.case_results[0].failure_category == "LEGACY_PYTEST_INFRASTRUCTURE"
    assert "exactly one Run directory" in (result.case_results[0].error_message or "")


def test_legacy_adapter_rejects_wrong_browser() -> None:
    job = legacy_job().model_copy(update={"browser": "firefox"})
    with tempfile.TemporaryDirectory() as temporary:
        project_root = Path(temporary) / "project"
        project_root.mkdir()
        create_project(project_root)
        adapter = LegacyPytestAdapter(
            workspace_root=Path(temporary) / "runs",
            project_root=project_root,
            secret_provider=MappingSecretProvider({}),
        )

        with pytest.raises(LegacyPytestValidationError, match="only supports chromium"):
            adapter.validate(job)


def test_legacy_adapter_accepts_headed_mode_and_passes_pytest_flag() -> None:
    base_job = legacy_job()
    assert base_job.web_config is not None
    job = base_job.model_copy(
        update={"web_config": base_job.web_config.model_copy(update={"headless": False})}
    )
    with tempfile.TemporaryDirectory() as temporary:
        project_root = Path(temporary) / "project"
        project_root.mkdir()
        create_project(project_root)
        adapter = LegacyPytestAdapter(
            workspace_root=Path(temporary) / "runs",
            project_root=project_root,
            secret_provider=MappingSecretProvider({}),
        )

        adapter.validate(job)
        command = adapter._command(job, job.cases)

    assert "--headed" in command
    assert command[command.index("-k") + 1] == "TC-LOGIN-001"


def test_legacy_adapter_accepts_governed_visual_model_configuration() -> None:
    base_job = legacy_job()
    job = base_job.model_copy(
        update={
            "variables": (
                RuntimeVariable(name="VISION_BASE_URL", value="https://vision.example/api"),
                RuntimeVariable(name="VISION_MODEL", value="qwen-vl"),
                RuntimeVariable(name="VISION_BASELINE_DIR", value="test_assets/vision_baselines"),
            ),
            "secret_bindings": (
                *base_job.secret_bindings,
                SecretBinding(name="VISION_API_KEY", ref="secret://yanjia/staging/vision_api_key"),
            ),
        }
    )
    with tempfile.TemporaryDirectory() as temporary:
        project_root = Path(temporary) / "project"
        project_root.mkdir()
        create_project(project_root)
        adapter = LegacyPytestAdapter(
            workspace_root=Path(temporary) / "runs",
            project_root=project_root,
            secret_provider=MappingSecretProvider({}),
        )

        adapter.validate(job)


def test_adapter_factory_dispatches_by_immutable_package_name() -> None:
    job = legacy_job()
    with tempfile.TemporaryDirectory() as temporary:
        project_root = Path(temporary) / "project"
        project_root.mkdir()
        create_project(project_root)
        adapter = create_automation_adapter(
            job,
            workspace_root=Path(temporary) / "runs",
            secret_provider=MappingSecretProvider({}),
            environ={"TESTOPS_LEGACY_PYTEST_PROJECT_ROOT": str(project_root)},
            playwright_adapter_type=PlaywrightWebAdapter,
            legacy_adapter_type=LegacyPytestAdapter,
        )

    assert isinstance(adapter, LegacyPytestAdapter)
