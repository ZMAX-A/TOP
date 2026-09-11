"""Run the existing YanJia Pytest project without rewriting its test implementation."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID

from testops.contracts import (
    CaseDefinition,
    CaseResult,
    CaseResultStatus,
    RunResult,
    RunSnapshot,
    RunStatus,
    TargetType,
)
from testops.runners.web.adapter import AutomationAdapter, ProgressReporter
from testops.runners.web.variables import SecretProvider
from testops.runners.web.workspace import RunWorkspace

from .runtime_workbook import materialize_runtime_workbook

LEGACY_PACKAGE_NAME = "yanjia-web-pytest"
LEGACY_RUNNER_VERSION = "1.0.0"
MAX_RESULT_BYTES = 5 * 1024 * 1024
MAX_LOG_CHARACTERS = 4 * 1024 * 1024
LEGACY_RERUN_ROUNDS = 2
LEGACY_RERUN_WAIT_SECONDS = 30
LEGACY_ORCHESTRATION_GRACE_SECONDS = 60
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PYTEST_RESULT_PATTERN = re.compile(
    r"(?P<case_code>TC-[A-Z0-9-]+).*?\b(?P<status>PASSED|FAILED|SKIPPED|ERROR)\b",
    re.IGNORECASE,
)
RERUN_ROUND_PATTERN = re.compile(
    r"第\s*(?P<round>[12])\s*轮复跑[：:].*?(?P<total>\d+)\s*条",
)
RERUN_RESULT_PATTERN = re.compile(
    r"\[(?P<case_code>TC-[A-Z0-9-]+)\].*?第\s*(?P<round>[12])\s*轮复跑"
    r"(?P<result>通过|仍失败)",
)
RERUN_ERROR_PATTERN = re.compile(
    r"\[(?P<case_code>TC-[A-Z0-9-]+)\].*?(?:复跑异常|未找到用例定义)",
)
REQUIRED_PROJECT_PATHS = (
    "pages",
    "test_assets",
    "tests/conftest.py",
    "tests/test_core_cases.py",
    "utils",
    "test_cases/test_case.xlsx",
    "pytest.ini",
    "run_parallel_tests.py",
)
ALLOWED_RUNTIME_VARIABLES = frozenset(
    {
        "ENV",
        "STORE_NAME",
        "STORE_NAME_A",
        "STORE_NAME_B",
        "TESTER",
        "TEST_SERIAL_ACCOUNT_SLOT",
        "VISION_BASE_URL",
        "VISION_MODEL",
        "VISION_TIMEOUT",
        "VISION_THINKING_BUDGET",
        "VISION_ENABLE_THINKING",
        "VISION_SYSTEM_PROMPT",
        "VISION_CACHE_ENABLED",
        "VISION_CACHE_MAX_ITEMS",
        "VISION_ATTACH_SCREENSHOT",
        "VISION_MIN_SCREENSHOT_BYTES",
        "VISION_BASELINE_DIR",
        "VISION_REQUIRE_BASELINE_METADATA",
        "VISION_RERUN_FAILURES",
    }
)
REQUIRED_SECRET_BINDINGS = frozenset(
    {"TEST_USERNAME_A", "TEST_PASSWORD_A", "TEST_USERNAME_B", "TEST_PASSWORD_B"}
)
OPTIONAL_SECRET_BINDINGS = frozenset({"VISION_API_KEY"})
SAFE_PARENT_ENVIRONMENT = frozenset(
    {
        "COMSPEC",
        "DISPLAY",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "PATH",
        "PATHEXT",
        "PLAYWRIGHT_BROWSERS_PATH",
        "PYTHONPATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "WINDIR",
        "XAUTHORITY",
    }
)


class LegacyPytestValidationError(ValueError):
    """The snapshot cannot be safely executed by this package adapter."""


class LegacyPytestExecutionError(RuntimeError):
    """The child Pytest process failed to produce a trustworthy result."""


class _CanceledBeforeExecution(Exception):
    """Internal control flow for cancellation requested by the event reporter."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    output: str
    duration_seconds: float


class CommandRunner(Protocol):
    def run(
        self,
        run_id: UUID,
        command: Sequence[str],
        *,
        cwd: Path,
        environ: Mapping[str, str],
        timeout_seconds: int,
        on_output: Callable[[str], None] | None = None,
    ) -> CommandResult: ...

    def cancel(self, run_id: UUID) -> None: ...


class SubprocessCommandRunner:
    """Execute one argument-vector command and retain a handle for cancellation."""

    def __init__(self) -> None:
        self._processes: dict[UUID, subprocess.Popen[str]] = {}
        self._lock = threading.Lock()

    def run(
        self,
        run_id: UUID,
        command: Sequence[str],
        *,
        cwd: Path,
        environ: Mapping[str, str],
        timeout_seconds: int,
        on_output: Callable[[str], None] | None = None,
    ) -> CommandResult:
        started = time.perf_counter()
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=dict(environ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        with self._lock:
            self._processes[run_id] = process
        output_lines: list[str] = []

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                output_lines.append(line)
                if on_output is not None:
                    try:
                        on_output(line.rstrip("\r\n"))
                    except Exception:
                        # Progress telemetry must never terminate the authoritative test process.
                        pass

        reader = threading.Thread(
            target=read_output,
            name=f"legacy-output-{run_id.hex[:8]}",
            daemon=True,
        )
        reader.start()
        try:
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait()
                raise LegacyPytestExecutionError(
                    f"legacy Pytest exceeded {timeout_seconds} seconds"
                ) from exc
        finally:
            reader.join(timeout=5)
            with self._lock:
                self._processes.pop(run_id, None)
        return CommandResult(
            returncode=process.returncode,
            output="".join(output_lines),
            duration_seconds=max(0.0, time.perf_counter() - started),
        )

    def cancel(self, run_id: UUID) -> None:
        with self._lock:
            process = self._processes.get(run_id)
        if process is not None and process.poll() is None:
            process.terminate()


class LegacyProgressTracker:
    """Translate the versioned legacy runner's console protocol into safe Run progress."""

    def __init__(self, case_codes: Sequence[str]) -> None:
        self._case_codes = frozenset(case_codes)
        self._statuses: dict[str, str] = {}
        self._round_finished: set[str] = set()
        self.current_round = 1
        self.total_rounds = 1 + LEGACY_RERUN_ROUNDS
        self.round_total = len(self._case_codes)
        self.current_case_code: str | None = None

    def initial_event(self, run_id: UUID, at: datetime) -> dict[str, object]:
        return self._event(run_id, at)

    def consume(self, run_id: UUID, line: str, at: datetime) -> dict[str, object] | None:
        normalized = ANSI_ESCAPE_PATTERN.sub("", line)
        round_match = RERUN_ROUND_PATTERN.search(normalized)
        if round_match is not None:
            self.current_round = int(round_match.group("round")) + 1
            self.round_total = int(round_match.group("total"))
            self._round_finished.clear()
            self.current_case_code = None
            return self._event(run_id, at)

        rerun_match = RERUN_RESULT_PATTERN.search(normalized)
        if rerun_match is not None:
            runner_round = int(rerun_match.group("round")) + 1
            case_code = rerun_match.group("case_code").upper()
            if runner_round != self.current_round or case_code not in self._case_codes:
                return None
            status = "PASSED" if rerun_match.group("result") == "通过" else "FAILED"
            return self._record(run_id, case_code, status, at)

        rerun_error = RERUN_ERROR_PATTERN.search(normalized)
        if rerun_error is not None and self.current_round > 1:
            case_code = rerun_error.group("case_code").upper()
            if case_code in self._case_codes:
                return self._record(run_id, case_code, "FAILED", at)
            return None

        if self.current_round != 1:
            return None
        result_match = PYTEST_RESULT_PATTERN.search(normalized)
        if result_match is None:
            return None
        case_code = result_match.group("case_code").upper()
        if case_code not in self._case_codes:
            return None
        raw_status = result_match.group("status").upper()
        status = {
            "PASSED": "PASSED",
            "SKIPPED": "SKIPPED",
            "FAILED": "FAILED",
            "ERROR": "FAILED",
        }[raw_status]
        return self._record(run_id, case_code, status, at)

    def finalize(
        self,
        run_id: UUID,
        results: Sequence[CaseResult],
        at: datetime,
    ) -> dict[str, object] | None:
        final_statuses = {result.case_code: result.status.value for result in results}
        before = self._progress_signature()
        self._statuses = final_statuses
        self._round_finished = {code for code in self._round_finished if code in final_statuses}
        if self.current_round == 1:
            self._round_finished = set(final_statuses)
            self.round_total = len(self._case_codes)
        self.current_case_code = next(reversed(final_statuses), None)
        if self._progress_signature() == before:
            return None
        return self._event(run_id, at)

    def _record(
        self,
        run_id: UUID,
        case_code: str,
        status: str,
        at: datetime,
    ) -> dict[str, object] | None:
        if case_code in self._round_finished:
            return None
        self._round_finished.add(case_code)
        self._statuses[case_code] = status
        self.current_case_code = case_code
        return self._event(run_id, at)

    def _progress_signature(self) -> tuple[object, ...]:
        return (
            self.current_round,
            self.round_total,
            frozenset(self._round_finished),
            frozenset(self._statuses.items()),
            self.current_case_code,
        )

    def _event(self, run_id: UUID, at: datetime) -> dict[str, object]:
        passed = sum(status == "PASSED" for status in self._statuses.values())
        skipped = sum(status == "SKIPPED" for status in self._statuses.values())
        failed = sum(
            status in {"FAILED", "INFRA_ERROR", "TIMED_OUT"} for status in self._statuses.values()
        )
        event: dict[str, object] = {
            "event": "progress_updated",
            "run_id": str(run_id),
            "at": at.isoformat(),
            "current_round": self.current_round,
            "total_rounds": self.total_rounds,
            "round_completed": len(self._round_finished),
            "round_total": self.round_total,
            "completed": len(self._statuses),
            "total": len(self._case_codes),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
        }
        if self.current_case_code is not None:
            event["current_case_code"] = self.current_case_code
        return event


class LegacyPytestAdapter(AutomationAdapter):
    """Bridge TOP snapshots to the existing Excel-driven Pytest suite."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        project_root: str | Path,
        secret_provider: SecretProvider,
        command_runner: CommandRunner | None = None,
        python_executable: str | Path = sys.executable,
        parent_environment: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._workspace_root = Path(workspace_root)
        self._project_root = Path(project_root).resolve()
        self._secret_provider = secret_provider
        self._command_runner = command_runner or SubprocessCommandRunner()
        self._python_executable = str(python_executable)
        self._parent_environment = os.environ if parent_environment is None else parent_environment
        self._clock = clock or (lambda: datetime.now(UTC))
        self._workspaces: dict[UUID, RunWorkspace] = {}
        self._artifact_uris: dict[UUID, tuple[str, ...]] = {}
        self._completed_runs: set[UUID] = set()
        self._canceled_runs: set[UUID] = set()
        self._state_lock = threading.Lock()

    def validate(self, job: RunSnapshot) -> None:
        if job.target_type != TargetType.WEB or job.web_config is None:
            raise LegacyPytestValidationError("LegacyPytestAdapter only accepts WEB jobs")
        if job.automation_package.name != LEGACY_PACKAGE_NAME:
            raise LegacyPytestValidationError(
                f"LegacyPytestAdapter requires automation package '{LEGACY_PACKAGE_NAME}'"
            )
        if job.browser != "chromium":
            raise LegacyPytestValidationError("legacy Pytest package only supports chromium")
        if job.web_config.ignore_https_errors:
            raise LegacyPytestValidationError(
                "legacy Pytest package does not support ignore_https_errors"
            )
        binding_names = {binding.name for binding in job.secret_bindings}
        missing = sorted(REQUIRED_SECRET_BINDINGS - binding_names)
        if missing:
            raise LegacyPytestValidationError(
                f"legacy Pytest package requires secret bindings: {', '.join(missing)}"
            )
        unsupported_bindings = sorted(
            binding_names - REQUIRED_SECRET_BINDINGS - OPTIONAL_SECRET_BINDINGS
        )
        if unsupported_bindings:
            raise LegacyPytestValidationError(
                "legacy Pytest package received unsupported secret bindings: "
                + ", ".join(unsupported_bindings)
            )
        variable_names = {variable.name for variable in job.variables}
        unsupported_variables = sorted(variable_names - ALLOWED_RUNTIME_VARIABLES)
        if unsupported_variables:
            raise LegacyPytestValidationError(
                "legacy Pytest package received unsupported runtime variables: "
                + ", ".join(unsupported_variables)
            )
        if not any(case.enabled for case in job.cases):
            raise LegacyPytestValidationError("legacy Pytest job requires an enabled case")
        missing_paths = [
            relative
            for relative in REQUIRED_PROJECT_PATHS
            if not (self._project_root / relative).exists()
        ]
        if missing_paths:
            raise LegacyPytestValidationError(
                "legacy Pytest project is incomplete: " + ", ".join(missing_paths)
            )

    def prepare(self, job: RunSnapshot) -> None:
        self.validate(job)
        with self._state_lock:
            if job.run_id in self._workspaces:
                raise RuntimeError(f"run is already prepared: {job.run_id}")
            workspace = RunWorkspace(self._workspace_root, job.run_id)
            runtime_project = self._copy_runtime_project(workspace)
            materialize_runtime_workbook(
                runtime_project / "test_cases" / "test_case.xlsx",
                job.cases,
                job.case_baseline,
                evidence_path=workspace.path / "runtime-workbook.json",
            )
            self._workspaces[job.run_id] = workspace

    def execute(self, job: RunSnapshot, reporter: ProgressReporter) -> RunResult:
        self.validate(job)
        with self._state_lock:
            if job.run_id in self._completed_runs:
                raise RuntimeError(f"run is already complete: {job.run_id}")
            workspace = self._workspaces.get(job.run_id)
        if workspace is None:
            self.prepare(job)
            workspace = self._workspaces[job.run_id]

        started_at = self._now()
        self._emit(
            workspace,
            reporter,
            {"event": "run_started", "run_id": str(job.run_id), "at": started_at.isoformat()},
        )
        enabled_cases = tuple(case for case in job.cases if case.enabled)
        disabled_results = [
            self._skipped_result(case, "case disabled") for case in job.cases if not case.enabled
        ]
        progress = LegacyProgressTracker(tuple(case.case_code for case in enabled_cases))
        self._emit(workspace, reporter, progress.initial_event(job.run_id, self._now()))
        secret_values: list[str] = []
        environment: dict[str, str] | None = None
        runtime_project = workspace.path / "_runtime-project"
        try:
            for case in enabled_cases:
                self._emit(
                    workspace,
                    reporter,
                    {
                        "event": "case_started",
                        "run_id": str(job.run_id),
                        "case_code": case.case_code,
                        "at": self._now().isoformat(),
                    },
                )
            if self._is_canceled(job.run_id):
                case_results = [
                    self._skipped_result(case, "run canceled") for case in enabled_cases
                ]
                raise _CanceledBeforeExecution
            environment, secret_values = self._child_environment(job, workspace)
            command = self._command(job, enabled_cases)

            def observe_output(line: str) -> None:
                event = progress.consume(job.run_id, line, self._now())
                if event is not None:
                    self._emit(workspace, reporter, event)

            command_result = self._command_runner.run(
                job.run_id,
                command,
                cwd=runtime_project,
                environ=environment,
                timeout_seconds=self._execution_timeout(enabled_cases),
                on_output=observe_output,
            )
            safe_output = self._redact(command_result.output, secret_values)
            (workspace.path / "parallel-runner.log").write_text(
                safe_output[-MAX_LOG_CHARACTERS:], encoding="utf-8"
            )
            case_results = self._parse_results(
                job,
                workspace,
                enabled_cases,
                started_at,
                command_result,
                secret_values,
                reporter,
                runtime_project,
            )
        except _CanceledBeforeExecution:
            pass
        except Exception as exc:
            if self._is_canceled(job.run_id):
                case_results = [
                    self._skipped_result(case, "run canceled") for case in enabled_cases
                ]
            else:
                safe_error = self._redact(str(exc), secret_values)[:2000]
                case_results = [self._infra_result(case, safe_error) for case in enabled_cases]
        finally:
            self._scrub_runtime_auth_state(runtime_project)
            self._scrub_runtime_home(workspace)
            if environment is not None:
                self._scrub_runtime_temp(environment)
        results_by_code = {
            result.case_code: result for result in (*case_results, *disabled_results)
        }
        ordered_results = [results_by_code[case.case_code] for case in job.cases]
        progress_event = progress.finalize(job.run_id, case_results, self._now())
        if progress_event is not None:
            self._emit(workspace, reporter, progress_event)

        status = self._terminal_status(job.run_id, ordered_results)
        finished_at = self._now()
        self._emit(
            workspace,
            reporter,
            {
                "event": "run_finished",
                "run_id": str(job.run_id),
                "status": status.value,
                "at": finished_at.isoformat(),
            },
        )
        artifacts = workspace.artifacts()
        result = RunResult(
            run_id=job.run_id,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            runner_version=LEGACY_RUNNER_VERSION,
            case_results=tuple(ordered_results),
            artifacts=artifacts,
        )
        workspace.write_result(result)
        with self._state_lock:
            self._artifact_uris[job.run_id] = tuple(artifact.uri for artifact in artifacts)
            self._completed_runs.add(job.run_id)
        return result

    def cancel(self, run_id: str) -> None:
        parsed = UUID(run_id)
        with self._state_lock:
            self._canceled_runs.add(parsed)
        self._command_runner.cancel(parsed)

    def collect(self, run_id: str) -> tuple[str, ...]:
        parsed = UUID(run_id)
        with self._state_lock:
            cached = self._artifact_uris.get(parsed)
            workspace = self._workspaces.get(parsed)
        if cached is not None:
            return cached
        if workspace is None:
            return ()
        return tuple(artifact.uri for artifact in workspace.artifacts())

    def health(self) -> dict[str, object]:
        return {
            "adapter": "LegacyPytestAdapter",
            "runner_version": LEGACY_RUNNER_VERSION,
            "package_name": LEGACY_PACKAGE_NAME,
            "project_ready": all(
                (self._project_root / relative).exists() for relative in REQUIRED_PROJECT_PATHS
            ),
        }

    def _child_environment(
        self, job: RunSnapshot, workspace: RunWorkspace
    ) -> tuple[dict[str, str], list[str]]:
        environment = {
            name: value
            for name in SAFE_PARENT_ENVIRONMENT
            if (value := self._parent_environment.get(name)) is not None
        }
        home = workspace.path / "home"
        home.mkdir()
        environment.update(
            {
                "BASE_URL": job.web_config.base_url,
                "HOME": str(home),
                "USERPROFILE": str(home),
                "PYTHONDONTWRITEBYTECODE": "1",
                "TEST_RUN_DIR": str(workspace.path),
                "TEST_RUN_ID": str(job.run_id),
                "TEST_RUN_TIMESTAMP": job.created_at.astimezone(UTC).strftime("%Y%m%d%H%M%S"),
                "TEST_SCREENSHOT_DIR": str(workspace.path / "screenshots"),
            }
        )
        for variable in job.variables:
            environment[variable.name] = variable.value
        secret_values: list[str] = []
        for binding in job.secret_bindings:
            value = self._secret_provider.resolve(binding)
            environment[binding.name] = value
            secret_values.append(value)
        runtime_temp = Path(
            tempfile.mkdtemp(
                prefix=f"testops-{job.run_id.hex[:8]}-",
                dir=self._runtime_temp_root(),
            )
        )
        environment.update(
            {
                "TEMP": str(runtime_temp),
                "TMP": str(runtime_temp),
                "TMPDIR": str(runtime_temp),
            }
        )
        return environment, secret_values

    def _command(
        self,
        job: RunSnapshot,
        cases: tuple[CaseDefinition, ...],
    ) -> tuple[str, ...]:
        expression = " or ".join(case.case_code for case in cases)
        command = (
            self._python_executable,
            "run_parallel_tests.py",
            "--skip-checks",
            "--no-report",
            "--no-open",
        )
        if job.web_config is not None and not job.web_config.headless:
            command += ("--headed",)
        return (*command, "-k", expression)

    def _parse_results(
        self,
        job: RunSnapshot,
        workspace: RunWorkspace,
        cases: tuple[CaseDefinition, ...],
        started_at: datetime,
        command_result: CommandResult,
        secret_values: list[str],
        reporter: ProgressReporter,
        runtime_project: Path,
    ) -> list[CaseResult]:
        reports_root = runtime_project / "reports" / "runs"
        run_directories = (
            sorted(path for path in reports_root.iterdir() if path.is_dir())
            if reports_root.is_dir()
            else []
        )
        if len(run_directories) != 1:
            raise LegacyPytestExecutionError(
                "legacy parallel runner did not produce exactly one Run directory"
            )
        legacy_run_dir = run_directories[0]
        result_path = legacy_run_dir / "summary.json"
        if not result_path.is_file() or result_path.stat().st_size > MAX_RESULT_BYTES:
            raise LegacyPytestExecutionError(
                "legacy parallel runner did not produce a bounded summary file"
            )
        try:
            payload = json.loads(result_path.read_text("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LegacyPytestExecutionError(
                "legacy parallel runner produced invalid summary JSON"
            ) from exc
        if not isinstance(payload, dict) or payload.get("mode") != "parallel-two-account":
            raise LegacyPytestExecutionError("legacy parallel runner summary mode is inconsistent")
        records = payload.get("results")
        if not isinstance(records, list):
            raise LegacyPytestExecutionError("legacy parallel runner result records are invalid")
        artifact_root = workspace.path / "parallel-results"
        shutil.copytree(
            legacy_run_dir,
            artifact_root,
            ignore=shutil.ignore_patterns("auth_state.json"),
        )
        records_by_code: dict[str, dict[str, object]] = {}
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("case_id"), str):
                raise LegacyPytestExecutionError("legacy Pytest result record is invalid")
            code = record["case_id"]
            if code in records_by_code:
                raise LegacyPytestExecutionError("legacy Pytest result contains duplicate cases")
            records_by_code[code] = record
        expected_codes = {case.case_code for case in cases}
        if set(records_by_code) != expected_codes:
            raise LegacyPytestExecutionError(
                "legacy Pytest result does not exactly match requested case codes"
            )
        parsed: list[CaseResult] = []
        cursor = started_at
        for case in cases:
            record = records_by_code[case.case_code]
            raw_status = str(record.get("status", "")).lower()
            status = {
                "pass": CaseResultStatus.PASSED,
                "fail": CaseResultStatus.FAILED,
                "skip": CaseResultStatus.SKIPPED,
                "infra_error": CaseResultStatus.INFRA_ERROR,
            }.get(raw_status)
            if status is None:
                raise LegacyPytestExecutionError("legacy Pytest result contains unknown status")
            try:
                duration_ms = max(0, round(float(record.get("duration", 0)) * 1000))
            except (TypeError, ValueError) as exc:
                raise LegacyPytestExecutionError(
                    "legacy Pytest result contains invalid duration"
                ) from exc
            finished = cursor + timedelta(milliseconds=duration_ms)
            message = self._redact(str(record.get("result", "")), secret_values)[:2000]
            failure_category = None
            error_message = None
            if status == CaseResultStatus.FAILED:
                failure_category = "PYTEST_FAILURE"
                error_message = message or "legacy Pytest case failed"
            elif status == CaseResultStatus.INFRA_ERROR:
                failure_category = "LEGACY_PYTEST_INFRASTRUCTURE"
                error_message = message or "legacy parallel Worker failed"
            elif status == CaseResultStatus.SKIPPED:
                failure_category = message or "legacy Pytest case skipped"
            result = CaseResult(
                case_id=case.case_id,
                case_code=case.case_code,
                status=status,
                started_at=cursor,
                finished_at=finished,
                duration_ms=duration_ms,
                failure_category=failure_category,
                error_message=error_message,
            )
            parsed.append(result)
            cursor = finished
            self._emit(
                workspace,
                reporter,
                {
                    "event": "case_finished",
                    "run_id": str(job.run_id),
                    "case_code": case.case_code,
                    "status": status.value,
                    "at": finished.isoformat(),
                },
            )
        if command_result.returncode != 0 and all(
            result.status in {CaseResultStatus.PASSED, CaseResultStatus.SKIPPED}
            for result in parsed
        ):
            raise LegacyPytestExecutionError(
                f"legacy Pytest exited with code {command_result.returncode}"
            )
        return parsed

    def _copy_runtime_project(self, workspace: RunWorkspace) -> Path:
        destination = workspace.path / "_runtime-project"
        destination.mkdir()
        for directory in ("pages", "tests", "utils"):
            shutil.copytree(
                self._project_root / directory,
                destination / directory,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
            )
        for relative in (
            "pytest.ini",
            "run_parallel_tests.py",
            "test_cases/test_case.xlsx",
        ):
            source = self._project_root / relative
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        for path in destination.rglob("*"):
            if path.is_file():
                path.chmod(path.stat().st_mode | stat.S_IWUSR)
        return destination

    @staticmethod
    def _scrub_runtime_auth_state(runtime_project: Path) -> None:
        if not runtime_project.is_dir():
            return
        for path in runtime_project.rglob("auth_state.json"):
            resolved = path.resolve()
            if runtime_project.resolve() not in resolved.parents:
                continue
            try:
                resolved.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _scrub_runtime_home(workspace: RunWorkspace) -> None:
        runtime_home = (workspace.path / "home").resolve()
        if workspace.path.resolve() not in runtime_home.parents:
            raise RuntimeError("runtime HOME escaped the Run workspace")
        shutil.rmtree(runtime_home, ignore_errors=True)

    @staticmethod
    def _runtime_temp_root() -> Path:
        posix_temp = Path("/tmp")
        if os.name == "posix" and posix_temp.is_dir():
            return posix_temp
        return Path(tempfile.gettempdir())

    @classmethod
    def _scrub_runtime_temp(cls, environment: Mapping[str, str]) -> None:
        runtime_temp_value = environment.get("TMPDIR")
        if not runtime_temp_value:
            return
        runtime_temp = Path(runtime_temp_value).resolve()
        temp_root = cls._runtime_temp_root().resolve()
        if temp_root not in runtime_temp.parents or not runtime_temp.name.startswith("testops-"):
            raise RuntimeError("runtime TEMP escaped the managed temporary root")
        shutil.rmtree(runtime_temp, ignore_errors=True)

    def _is_canceled(self, run_id: UUID) -> bool:
        with self._state_lock:
            return run_id in self._canceled_runs

    @staticmethod
    def _execution_timeout(cases: tuple[CaseDefinition, ...]) -> int:
        case_budget = sum(math.ceil(case.timeout_seconds or 300) for case in cases)
        seconds = (
            case_budget * (1 + LEGACY_RERUN_ROUNDS)
            + LEGACY_RERUN_WAIT_SECONDS * LEGACY_RERUN_ROUNDS
            + LEGACY_ORCHESTRATION_GRACE_SECONDS
        )
        return max(60, min(seconds, 6 * 60 * 60))

    def _infra_result(self, case: CaseDefinition, message: str) -> CaseResult:
        moment = self._now()
        return CaseResult(
            case_id=case.case_id,
            case_code=case.case_code,
            status=CaseResultStatus.INFRA_ERROR,
            started_at=moment,
            finished_at=moment,
            duration_ms=0,
            failure_category="LEGACY_PYTEST_INFRASTRUCTURE",
            error_message=message or "legacy Pytest infrastructure error",
        )

    def _skipped_result(self, case: CaseDefinition, reason: str) -> CaseResult:
        moment = self._now()
        return CaseResult(
            case_id=case.case_id,
            case_code=case.case_code,
            status=CaseResultStatus.SKIPPED,
            started_at=moment,
            finished_at=moment,
            duration_ms=0,
            failure_category=reason,
        )

    def _terminal_status(self, run_id: UUID, results: list[CaseResult]) -> RunStatus:
        with self._state_lock:
            if run_id in self._canceled_runs:
                return RunStatus.CANCELED
        statuses = {result.status for result in results}
        if CaseResultStatus.INFRA_ERROR in statuses:
            return RunStatus.INFRA_ERROR
        if CaseResultStatus.FAILED in statuses:
            return RunStatus.FAILED
        return RunStatus.PASSED

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("Runner clock must return timezone-aware datetimes")
        return value

    @staticmethod
    def _redact(value: str, secrets: list[str]) -> str:
        redacted = value
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                redacted = redacted.replace(secret, "***")
        return redacted

    @staticmethod
    def _emit(
        workspace: RunWorkspace,
        reporter: ProgressReporter,
        event: dict[str, object],
    ) -> None:
        workspace.append_event(event)
        try:
            reporter(dict(event))
        except Exception:
            pass
