"""Execute immutable YanJia Android Excel cases on a Windows device Worker."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
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

from .package_digest import package_source_digest

ANDROID_PACKAGE_NAME = "yanjia-android-appium"
ANDROID_RUNNER_VERSION = "1.0.0"
MAX_LOG_CHARACTERS = 4 * 1024 * 1024
MAX_RESULT_BYTES = 5 * 1024 * 1024
RETRY_WAIT_SECONDS = 30
CASE_PROGRESS_PATTERN = re.compile(
    r"(?P<case_code>TC-[A-Z0-9-]+).*?\b(?P<status>PASSED|FAILED|SKIPPED|ERROR)\b",
    re.IGNORECASE,
)
REQUIRED_PROJECT_PATHS = (
    ".venv/Scripts/python.exe",
    "scripts/run-excel.ps1",
    "test_case.xlsx",
    "testops-package.json",
)
ALLOWED_RUNTIME_VARIABLES = frozenset(
    {
        "ANDROID_UDID",
        "APPIUM_SERVER_URL",
        "APPIUM_STARTUP_TIMEOUT_SECONDS",
        "YANJIA_APP_PACKAGE",
        "YANJIA_ALLOW_MUTATION",
        "YANJIA_ALLOW_DESTRUCTIVE",
    }
)
ALLOWED_SECRET_BINDINGS = frozenset(
    {
        "YANJIA_USERNAME",
        "YANJIA_PASSWORD",
        "SEEDED_CUSTOMER_PHONE",
        "YANJIA_MUTATION_CUSTOMER_QUERY",
    }
)
REQUIRED_AUTH_BINDINGS = frozenset({"YANJIA_USERNAME", "YANJIA_PASSWORD"})
SAFE_PARENT_ENVIRONMENT = frozenset(
    {
        "ANDROID_HOME",
        "ANDROID_SDK_ROOT",
        "COMSPEC",
        "JAVA_HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
)
CONTROL_VARIABLES = frozenset({"YANJIA_ALLOW_MUTATION", "YANJIA_ALLOW_DESTRUCTIVE"})


class AndroidAppiumValidationError(ValueError):
    """The snapshot cannot be safely executed by the Android package."""


class AndroidAppiumExecutionError(RuntimeError):
    """The Android process failed to produce a trustworthy Allure result."""


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
            shell=False,
        )
        with self._lock:
            self._processes[run_id] = process
        lines: list[str] = []

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.append(line)
                if on_output is not None:
                    try:
                        on_output(line.rstrip("\r\n"))
                    except Exception:
                        pass

        reader = threading.Thread(
            target=read_output,
            name=f"android-output-{run_id.hex[:8]}",
            daemon=True,
        )
        reader.start()
        try:
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait()
                raise AndroidAppiumExecutionError(
                    f"Android Appium execution exceeded {timeout_seconds} seconds"
                ) from exc
        finally:
            reader.join(timeout=5)
            with self._lock:
                self._processes.pop(run_id, None)
        return CommandResult(
            returncode=process.returncode,
            output="".join(lines),
            duration_seconds=max(0.0, time.perf_counter() - started),
        )

    def cancel(self, run_id: UUID) -> None:
        with self._lock:
            process = self._processes.get(run_id)
        if process is not None and process.poll() is None:
            process.terminate()


class AndroidAppiumAdapter(AutomationAdapter):
    """Bridge an APP Run Snapshot to the local Excel/Appium launcher."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        project_root: str | Path,
        secret_provider: SecretProvider,
        command_runner: CommandRunner | None = None,
        powershell_executable: str | Path | None = None,
        parent_environment: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        self._workspace_root = Path(workspace_root)
        self._project_root = Path(project_root).resolve()
        self._secret_provider = secret_provider
        self._command_runner = command_runner or SubprocessCommandRunner()
        self._powershell_executable = str(
            powershell_executable
            or shutil.which("pwsh")
            or shutil.which("powershell.exe")
            or "powershell.exe"
        )
        self._parent_environment = os.environ if parent_environment is None else parent_environment
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleeper = sleeper or time.sleep
        self._workspaces: dict[UUID, RunWorkspace] = {}
        self._artifact_uris: dict[UUID, tuple[str, ...]] = {}
        self._completed_runs: set[UUID] = set()
        self._canceled_runs: set[UUID] = set()
        self._state_lock = threading.Lock()

    def validate(self, job: RunSnapshot) -> None:
        if (
            job.target_type != TargetType.APP
            or job.web_config is not None
            or job.browser is not None
        ):
            raise AndroidAppiumValidationError("AndroidAppiumAdapter only accepts APP jobs")
        package = job.automation_package
        if package.runner_type != "ANDROID_APPIUM" or package.name != ANDROID_PACKAGE_NAME:
            raise AndroidAppiumValidationError(
                f"Android jobs require the '{ANDROID_PACKAGE_NAME}' ANDROID_APPIUM package"
            )
        missing_paths = [
            relative
            for relative in REQUIRED_PROJECT_PATHS
            if not (self._project_root / relative).is_file()
        ]
        if missing_paths:
            raise AndroidAppiumValidationError(
                "Android package is incomplete: " + ", ".join(missing_paths)
            )
        self._validate_manifest()
        actual_digest = package_source_digest(self._project_root)
        if actual_digest.lower() != package.digest.lower():
            raise AndroidAppiumValidationError(
                "Android package digest does not match the mounted source"
            )
        binding_names = {binding.name for binding in job.secret_bindings}
        missing_auth = sorted(REQUIRED_AUTH_BINDINGS - binding_names)
        if missing_auth:
            raise AndroidAppiumValidationError(
                "Android package requires secret bindings: " + ", ".join(missing_auth)
            )
        unsupported_bindings = sorted(binding_names - ALLOWED_SECRET_BINDINGS)
        if unsupported_bindings:
            raise AndroidAppiumValidationError(
                "Android package received unsupported secret bindings: "
                + ", ".join(unsupported_bindings)
            )
        variable_names = {variable.name for variable in job.variables}
        unsupported_variables = sorted(variable_names - ALLOWED_RUNTIME_VARIABLES)
        if unsupported_variables:
            raise AndroidAppiumValidationError(
                "Android package received unsupported runtime variables: "
                + ", ".join(unsupported_variables)
            )
        enabled_cases = tuple(case for case in job.cases if case.enabled)
        if not enabled_cases:
            raise AndroidAppiumValidationError("Android job requires an enabled case")
        for case in enabled_cases:
            self._validate_case_contract(case)
        tags = {tag.lower() for case in enabled_cases for tag in case.tags}
        variables = {variable.name: variable.value for variable in job.variables}
        if tags.intersection({"mutating", "destructive", "persistent"}):
            if variables.get("YANJIA_ALLOW_MUTATION", "").lower() != "true":
                raise AndroidAppiumValidationError(
                    "mutating Android cases require YANJIA_ALLOW_MUTATION=true"
                )
            required = {"SEEDED_CUSTOMER_PHONE", "YANJIA_MUTATION_CUSTOMER_QUERY"}
            missing = sorted(required - binding_names)
            if missing:
                raise AndroidAppiumValidationError(
                    "mutating Android cases require secret bindings: " + ", ".join(missing)
                )
        if (
            "destructive" in tags
            and variables.get("YANJIA_ALLOW_DESTRUCTIVE", "").lower() != "true"
        ):
            raise AndroidAppiumValidationError(
                "destructive Android cases require YANJIA_ALLOW_DESTRUCTIVE=true"
            )
        if any(case.case_code == "TC-HOME-007" for case in enabled_cases):
            if "SEEDED_CUSTOMER_PHONE" not in binding_names:
                raise AndroidAppiumValidationError(
                    "TC-HOME-007 requires SEEDED_CUSTOMER_PHONE as a secret binding"
                )

    def prepare(self, job: RunSnapshot) -> None:
        self.validate(job)
        with self._state_lock:
            if job.run_id in self._workspaces:
                raise RuntimeError(f"run is already prepared: {job.run_id}")
            self._workspaces[job.run_id] = RunWorkspace(self._workspace_root, job.run_id)

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
        enabled = tuple(case for case in job.cases if case.enabled)
        disabled_results = [
            self._skipped_result(case, "case disabled") for case in job.cases if not case.enabled
        ]
        environment: dict[str, str] | None = None
        secret_values: list[str] = []
        case_results: list[CaseResult]
        try:
            environment, secret_values = self._child_environment(job)
            if self._requires_preflight(enabled):
                self._run_preflight(job, workspace, environment, secret_values, reporter)
            first_results = self._run_round(
                job,
                workspace,
                enabled,
                environment,
                secret_values,
                reporter,
                round_number=1,
            )
            retry_cases = tuple(
                case
                for case in enabled
                if self._is_retryable(case)
                and next(
                    result for result in first_results if result.case_code == case.case_code
                ).status
                in {CaseResultStatus.FAILED, CaseResultStatus.INFRA_ERROR}
            )
            results_by_code = {result.case_code: result for result in first_results}
            if retry_cases and not self._is_canceled(job.run_id):
                self._emit(
                    workspace,
                    reporter,
                    {
                        "event": "retry_scheduled",
                        "run_id": str(job.run_id),
                        "at": self._now().isoformat(),
                        "wait_seconds": RETRY_WAIT_SECONDS,
                        "case_codes": [case.case_code for case in retry_cases],
                    },
                )
                self._sleeper(RETRY_WAIT_SECONDS)
                retried = self._run_round(
                    job,
                    workspace,
                    retry_cases,
                    environment,
                    secret_values,
                    reporter,
                    round_number=2,
                    read_only_retry=True,
                )
                results_by_code.update({result.case_code: result for result in retried})
            case_results = [results_by_code[case.case_code] for case in enabled]
        except Exception as exc:
            safe_error = self._redact(str(exc), secret_values)[:2000]
            if self._is_canceled(job.run_id):
                case_results = [self._skipped_result(case, "run canceled") for case in enabled]
            else:
                case_results = [self._infra_result(case, safe_error) for case in enabled]
        finally:
            if environment is not None:
                environment.clear()

        by_code = {result.case_code: result for result in (*case_results, *disabled_results)}
        ordered_results = [by_code[case.case_code] for case in job.cases]
        for result in ordered_results:
            self._emit(
                workspace,
                reporter,
                {
                    "event": "case_finished",
                    "run_id": str(job.run_id),
                    "case_code": result.case_code,
                    "status": result.status.value,
                    "at": result.finished_at.isoformat(),
                },
            )
        finished_at = self._now()
        artifacts = workspace.artifacts()
        result = RunResult(
            run_id=job.run_id,
            status=self._terminal_status(job.run_id, ordered_results),
            started_at=started_at,
            finished_at=finished_at,
            runner_version=ANDROID_RUNNER_VERSION,
            case_results=tuple(ordered_results),
            artifacts=artifacts,
        )
        workspace.write_result(result)
        with self._state_lock:
            self._completed_runs.add(job.run_id)
            self._artifact_uris[job.run_id] = tuple(artifact.uri for artifact in artifacts)
        return result

    def cancel(self, run_id: str) -> None:
        parsed = UUID(run_id)
        with self._state_lock:
            if parsed in self._completed_runs:
                return
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
            "adapter": "AndroidAppiumAdapter",
            "runner_version": ANDROID_RUNNER_VERSION,
            "package_name": ANDROID_PACKAGE_NAME,
            "project_ready": all(
                (self._project_root / relative).is_file() for relative in REQUIRED_PROJECT_PATHS
            ),
            "platform": os.name,
        }

    def _validate_manifest(self) -> None:
        try:
            payload = json.loads((self._project_root / "testops-package.json").read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AndroidAppiumValidationError("invalid Android package manifest") from exc
        expected = {
            "name": ANDROID_PACKAGE_NAME,
            "version": ANDROID_RUNNER_VERSION,
            "runner_type": "ANDROID_APPIUM",
            "target_type": "APP",
            "entrypoint": "scripts/run-excel.ps1",
            "workbook": "test_case.xlsx",
        }
        for field, value in expected.items():
            if payload.get(field) != value:
                raise AndroidAppiumValidationError(
                    f"Android package manifest field {field} must be {value!r}"
                )

    @staticmethod
    def _validate_case_contract(case: CaseDefinition) -> None:
        if len(case.steps) != 1:
            raise AndroidAppiumValidationError(
                f"{case.case_code} must contain exactly one managed Excel operation"
            )
        step = case.steps[0]
        if step.operation != "execute_excel_case" or step.input != case.case_code:
            raise AndroidAppiumValidationError(
                f"{case.case_code} does not match the managed Excel operation contract"
            )
        if case.assertion.type != "excel_case_passed":
            raise AndroidAppiumValidationError(
                f"{case.case_code} does not use the managed Excel assertion"
            )

    def _child_environment(self, job: RunSnapshot) -> tuple[dict[str, str], list[str]]:
        environment = {
            name: value
            for name in SAFE_PARENT_ENVIRONMENT
            if (value := self._parent_environment.get(name)) is not None
        }
        environment.update(
            {
                "ALLURE_NO_ANALYTICS": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
                "PYTHONUTF8": "1",
                "YANJIA_IGNORE_DOTENV": "true",
            }
        )
        for variable in job.variables:
            if variable.name not in CONTROL_VARIABLES:
                environment[variable.name] = variable.value
        secret_values: list[str] = []
        for binding in job.secret_bindings:
            value = self._secret_provider.resolve(binding)
            environment[binding.name] = value
            secret_values.append(value)
        return environment, secret_values

    def _run_preflight(
        self,
        job: RunSnapshot,
        workspace: RunWorkspace,
        environment: Mapping[str, str],
        secrets: list[str],
        reporter: ProgressReporter,
    ) -> None:
        if self._is_canceled(job.run_id):
            raise AndroidAppiumExecutionError("run canceled before customer preflight")
        self._emit(
            workspace,
            reporter,
            {
                "event": "preflight_started",
                "run_id": str(job.run_id),
                "at": self._now().isoformat(),
            },
        )
        run_name = f"{job.run_id}-preflight"
        command = self._command(
            job,
            ("TC-HOME-007",),
            workspace,
            run_name,
            mutation=False,
            destructive=False,
            preflight_verified=False,
            read_only_retry=False,
        )
        result = self._command_runner.run(
            job.run_id,
            command,
            cwd=self._project_root,
            environ=environment,
            timeout_seconds=900,
        )
        self._write_log(workspace, run_name, result.output, secrets)
        parsed = self._read_allure_records(
            workspace,
            run_name,
            {"TC-HOME-007": UUID(int=0)},
            secrets,
        )
        if result.returncode != 0 or parsed["TC-HOME-007"].status != CaseResultStatus.PASSED:
            raise AndroidAppiumExecutionError("dedicated customer uniqueness preflight failed")
        self._emit(
            workspace,
            reporter,
            {"event": "preflight_passed", "run_id": str(job.run_id), "at": self._now().isoformat()},
        )

    def _run_round(
        self,
        job: RunSnapshot,
        workspace: RunWorkspace,
        cases: tuple[CaseDefinition, ...],
        environment: Mapping[str, str],
        secrets: list[str],
        reporter: ProgressReporter,
        *,
        round_number: int,
        read_only_retry: bool = False,
    ) -> list[CaseResult]:
        if self._is_canceled(job.run_id):
            return [self._skipped_result(case, "run canceled") for case in cases]
        run_name = f"{job.run_id}-round-{round_number}"
        case_codes = tuple(case.case_code for case in cases)
        for case in cases:
            self._emit(
                workspace,
                reporter,
                {
                    "event": "case_started",
                    "run_id": str(job.run_id),
                    "case_code": case.case_code,
                    "round": round_number,
                    "at": self._now().isoformat(),
                },
            )

        def observe_output(line: str) -> None:
            match = CASE_PROGRESS_PATTERN.search(line)
            if match is None or match.group("case_code").upper() not in case_codes:
                return
            self._emit(
                workspace,
                reporter,
                {
                    "event": "progress_updated",
                    "run_id": str(job.run_id),
                    "current_case_code": match.group("case_code").upper(),
                    "current_round": round_number,
                    "total_rounds": 2,
                    "at": self._now().isoformat(),
                },
            )

        command = self._command(
            job,
            case_codes,
            workspace,
            run_name,
            mutation=self._requires_preflight(cases),
            destructive=self._has_tag(cases, "destructive"),
            preflight_verified=self._requires_preflight(cases),
            read_only_retry=read_only_retry,
        )
        command_result = self._command_runner.run(
            job.run_id,
            command,
            cwd=self._project_root,
            environ=environment,
            timeout_seconds=self._execution_timeout(cases),
            on_output=observe_output,
        )
        self._write_log(workspace, run_name, command_result.output, secrets)
        records = self._read_allure_records(
            workspace,
            run_name,
            {case.case_code: case.case_id for case in cases},
            secrets,
        )
        results = [records[case.case_code] for case in cases]
        if command_result.returncode != 0 and all(
            result.status in {CaseResultStatus.PASSED, CaseResultStatus.SKIPPED}
            for result in results
        ):
            raise AndroidAppiumExecutionError(
                f"Android Excel runner exited with code {command_result.returncode}"
            )
        return results

    def _command(
        self,
        job: RunSnapshot,
        case_codes: tuple[str, ...],
        workspace: RunWorkspace,
        run_name: str,
        *,
        mutation: bool,
        destructive: bool,
        preflight_verified: bool,
        read_only_retry: bool,
    ) -> tuple[str, ...]:
        command = (
            self._powershell_executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self._project_root / "scripts" / "run-excel.ps1"),
            "-ExcelFile",
            str(self._project_root / "test_case.xlsx"),
            "-ReportsRoot",
            str(workspace.path / "android-reports"),
            "-RunId",
            run_name,
            "-CaseId",
            ",".join(case_codes),
            "-RunSeeded",
            "-NoWriteBack",
            "-SkipExcelValidation",
            "-VerboseProgress",
            "-IgnoreDotEnv",
            "-AppiumProcessIdFile",
            str(workspace.path / "appium.pid"),
        )
        if mutation:
            command += ("-AllowMutation",)
        if destructive:
            command += ("-AllowDestructive",)
        if preflight_verified:
            command += ("-CustomerPreflightVerified",)
        if read_only_retry:
            command += ("-ReadOnlyRetry",)
        return command

    def _read_allure_records(
        self,
        workspace: RunWorkspace,
        run_name: str,
        expected_cases: Mapping[str, UUID],
        secrets: list[str],
    ) -> dict[str, CaseResult]:
        result_root = workspace.path / "android-reports" / "allure-results" / run_name
        files = sorted(result_root.glob("*-result.json")) if result_root.is_dir() else []
        if not files:
            raise AndroidAppiumExecutionError("Android runner produced no Allure result files")
        records: dict[str, CaseResult] = {}
        for path in files:
            if path.stat().st_size > MAX_RESULT_BYTES:
                raise AndroidAppiumExecutionError("Android Allure result exceeds the size limit")
            try:
                payload = json.loads(path.read_text("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AndroidAppiumExecutionError(
                    "Android runner produced invalid Allure JSON"
                ) from exc
            labels = payload.get("labels", [])
            case_code = next(
                (
                    str(item.get("value", "")).upper()
                    for item in labels
                    if isinstance(item, dict) and item.get("name") == "case_id"
                ),
                "",
            )
            if case_code not in expected_cases:
                continue
            if case_code in records:
                raise AndroidAppiumExecutionError(
                    f"Android runner produced duplicate results for {case_code}"
                )
            records[case_code] = self._allure_case_result(
                payload,
                case_code,
                expected_cases[case_code],
                secrets,
            )
        if set(records) != set(expected_cases):
            missing = ", ".join(sorted(set(expected_cases) - set(records)))
            raise AndroidAppiumExecutionError(
                f"Android runner result does not match requested cases; missing: {missing}"
            )
        return records

    def _allure_case_result(
        self,
        payload: Mapping[str, object],
        case_code: str,
        case_id: UUID,
        secrets: list[str],
    ) -> CaseResult:
        raw_status = str(payload.get("status", "")).lower()
        status = {
            "passed": CaseResultStatus.PASSED,
            "failed": CaseResultStatus.FAILED,
            "broken": CaseResultStatus.INFRA_ERROR,
            "skipped": CaseResultStatus.SKIPPED,
        }.get(raw_status)
        if status is None:
            raise AndroidAppiumExecutionError(f"{case_code} has an unknown Allure status")
        start_ms = self._integer_timestamp(payload.get("start"))
        stop_ms = self._integer_timestamp(payload.get("stop"))
        now = self._now()
        started_at = datetime.fromtimestamp(start_ms / 1000, UTC) if start_ms else now
        finished_at = datetime.fromtimestamp(stop_ms / 1000, UTC) if stop_ms else started_at
        if finished_at < started_at:
            finished_at = started_at
        details = payload.get("statusDetails")
        message = ""
        if isinstance(details, dict):
            message = str(details.get("message") or details.get("trace") or "")
        message = self._redact(message, secrets)[:2000]
        failure_category: str | None = None
        error_message: str | None = None
        if status == CaseResultStatus.FAILED:
            failure_category = "ANDROID_ASSERTION_FAILURE"
            error_message = message or "Android case assertion failed"
        elif status == CaseResultStatus.INFRA_ERROR:
            failure_category = "ANDROID_APPIUM_INFRASTRUCTURE"
            error_message = message or "Android Appium infrastructure failed"
        elif status == CaseResultStatus.SKIPPED:
            failure_category = message or "Android case skipped"
        return CaseResult(
            case_id=case_id,
            case_code=case_code,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=max(0, round((finished_at - started_at).total_seconds() * 1000)),
            failure_category=failure_category,
            error_message=error_message,
        )

    def _write_log(
        self, workspace: RunWorkspace, run_name: str, output: str, secrets: list[str]
    ) -> None:
        logs = workspace.path / "logs"
        logs.mkdir(exist_ok=True)
        (logs / f"{run_name}.log").write_text(
            self._redact(output, secrets)[-MAX_LOG_CHARACTERS:], encoding="utf-8"
        )

    @staticmethod
    def _integer_timestamp(value: object) -> int | None:
        try:
            result = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return result if result > 0 else None

    @staticmethod
    def _has_tag(cases: Sequence[CaseDefinition], tag: str) -> bool:
        expected = tag.lower()
        return any(expected in {item.lower() for item in case.tags} for case in cases)

    @classmethod
    def _requires_preflight(cls, cases: Sequence[CaseDefinition]) -> bool:
        return any(
            cls._has_tag(cases, tag)
            for tag in ("mutating", "destructive", "persistent")
        )

    @staticmethod
    def _is_retryable(case: CaseDefinition) -> bool:
        tags = {tag.lower() for tag in case.tags}
        return "readonly" in tags and not tags.intersection(
            {"mutating", "destructive", "persistent", "no_retry"}
        )

    @staticmethod
    def _execution_timeout(cases: tuple[CaseDefinition, ...]) -> int:
        case_budget = sum(math.ceil(case.timeout_seconds or 600) for case in cases)
        return max(900, min(case_budget + len(cases) * 300 + 300, 24 * 60 * 60))

    def _infra_result(self, case: CaseDefinition, message: str) -> CaseResult:
        moment = self._now()
        return CaseResult(
            case_id=case.case_id,
            case_code=case.case_code,
            status=CaseResultStatus.INFRA_ERROR,
            started_at=moment,
            finished_at=moment,
            duration_ms=0,
            failure_category="ANDROID_APPIUM_INFRASTRUCTURE",
            error_message=message or "Android Appium infrastructure error",
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

    def _terminal_status(self, run_id: UUID, results: Sequence[CaseResult]) -> RunStatus:
        if self._is_canceled(run_id):
            return RunStatus.CANCELED
        statuses = {result.status for result in results}
        if CaseResultStatus.INFRA_ERROR in statuses:
            return RunStatus.INFRA_ERROR
        if CaseResultStatus.FAILED in statuses:
            return RunStatus.FAILED
        return RunStatus.PASSED

    def _is_canceled(self, run_id: UUID) -> bool:
        with self._state_lock:
            return run_id in self._canceled_runs

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
        workspace: RunWorkspace, reporter: ProgressReporter, event: dict[str, object]
    ) -> None:
        workspace.append_event(event)
        try:
            reporter(dict(event))
        except Exception:
            pass
