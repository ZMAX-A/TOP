"""Structured Web Runner adapter for immutable Run Snapshot jobs."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
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

from .adapter import AutomationAdapter, ProgressReporter
from .browser_backend import BrowserBackend, PlaywrightBrowserBackend
from .engine import (
    AssertionExecutionError,
    RunnerJobValidationError,
    StepExecutionError,
    WebCaseEngine,
)
from .variables import SecretProvider, SecretResolutionError, VariableResolver
from .workspace import RunWorkspace

RUNNER_VERSION = "0.1.0"


class PlaywrightWebAdapter(AutomationAdapter):
    """Execute the supported Web capability slice with per-run isolation."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        secret_provider: SecretProvider,
        browser_backend: BrowserBackend | None = None,
        engine: WebCaseEngine | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._workspace_root = Path(workspace_root)
        self._secret_provider = secret_provider
        self._browser_backend = browser_backend or PlaywrightBrowserBackend()
        self._engine = engine or WebCaseEngine()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._workspaces: dict[UUID, RunWorkspace] = {}
        self._artifact_uris: dict[UUID, tuple[str, ...]] = {}
        self._completed_runs: set[UUID] = set()
        self._canceled_runs: set[UUID] = set()
        self._state_lock = threading.Lock()

    def validate(self, job: RunSnapshot) -> None:
        if job.target_type != TargetType.WEB:
            raise RunnerJobValidationError("PlaywrightWebAdapter only accepts WEB jobs")
        if job.web_config is None:
            raise RunnerJobValidationError("WEB job requires web_config")
        if job.browser not in {"chromium", "firefox", "webkit"}:
            raise RunnerJobValidationError(f"unsupported browser: {job.browser}")
        for case in job.cases:
            if case.module_key != "login":
                raise RunnerJobValidationError(
                    f"runner version {RUNNER_VERSION} only supports the login module; "
                    f"received {case.case_code} ({case.module_key})"
                )
            self._engine.validate_case(case)

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

        resolver: VariableResolver | None = None
        case_results: list[CaseResult] = []
        try:
            resolver = VariableResolver(job, self._secret_provider)
        except SecretResolutionError as exc:
            safe_error = str(exc)
            case_results.extend(self._remaining_infra_results(job.cases, safe_error))
        else:
            try:
                with self._browser_backend.start(job, workspace) as browser_run:
                    for case in job.cases:
                        if self._is_canceled(job.run_id):
                            case_results.append(self._skipped_result(case, "run canceled"))
                            continue
                        if not case.enabled:
                            case_results.append(self._skipped_result(case, "case disabled"))
                            continue
                        case_results.append(
                            self._execute_case(
                                job,
                                case,
                                browser_run,
                                resolver,
                                workspace,
                                reporter,
                            )
                        )
            except Exception as exc:
                safe_error = resolver.redact(str(exc))[:2000]
                completed_ids = {result.case_id for result in case_results}
                remaining = [case for case in job.cases if case.case_id not in completed_ids]
                case_results.extend(self._remaining_infra_results(remaining, safe_error))

        status = self._terminal_status(job.run_id, case_results)
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
            runner_version=RUNNER_VERSION,
            case_results=tuple(case_results),
            artifacts=artifacts,
        )
        workspace.write_result(result)
        with self._state_lock:
            self._artifact_uris[job.run_id] = tuple(artifact.uri for artifact in artifacts)
            self._completed_runs.add(job.run_id)
        return result

    def _execute_case(
        self,
        job: RunSnapshot,
        case: CaseDefinition,
        browser_run: object,
        resolver: VariableResolver,
        workspace: RunWorkspace,
        reporter: ProgressReporter,
    ) -> CaseResult:
        started_at = self._now()
        started_tick = time.perf_counter()
        self._emit(
            workspace,
            reporter,
            {
                "event": "case_started",
                "run_id": str(job.run_id),
                "case_code": case.case_code,
                "at": started_at.isoformat(),
            },
        )
        if self._is_canceled(job.run_id):
            result = self._skipped_result(case, "run canceled")
            self._emit(
                workspace,
                reporter,
                {
                    "event": "case_finished",
                    "run_id": str(job.run_id),
                    "case_code": case.case_code,
                    "status": result.status.value,
                    "at": result.finished_at.isoformat(),
                },
            )
            return result
        status = CaseResultStatus.PASSED
        failure_category: str | None = None
        error_message: str | None = None
        try:
            case_page = browser_run.case_page
            with case_page(case.case_code) as page:
                self._engine.execute_case(page, case, job.web_config, resolver)
        except AssertionExecutionError as exc:
            status = CaseResultStatus.FAILED
            failure_category = "ASSERTION"
            error_message = resolver.redact(str(exc))[:2000]
        except (StepExecutionError, SecretResolutionError) as exc:
            status = CaseResultStatus.FAILED
            failure_category = "STEP"
            error_message = resolver.redact(str(exc))[:2000]
        except Exception as exc:
            status = CaseResultStatus.INFRA_ERROR
            failure_category = "INFRASTRUCTURE"
            error_message = resolver.redact(str(exc))[:2000]
        finished_at = self._now()
        duration_ms = max(0, round((time.perf_counter() - started_tick) * 1000))
        self._emit(
            workspace,
            reporter,
            {
                "event": "case_finished",
                "run_id": str(job.run_id),
                "case_code": case.case_code,
                "status": status.value,
                "at": finished_at.isoformat(),
            },
        )
        return CaseResult(
            case_id=case.case_id,
            case_code=case.case_code,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            failure_category=failure_category,
            error_message=error_message,
        )

    def _remaining_infra_results(
        self,
        cases: list[CaseDefinition] | tuple[CaseDefinition, ...],
        error_message: str,
    ) -> list[CaseResult]:
        results: list[CaseResult] = []
        for case in cases:
            if not case.enabled:
                results.append(self._skipped_result(case, "case disabled"))
                continue
            moment = self._now()
            results.append(
                CaseResult(
                    case_id=case.case_id,
                    case_code=case.case_code,
                    status=CaseResultStatus.INFRA_ERROR,
                    started_at=moment,
                    finished_at=moment,
                    duration_ms=0,
                    failure_category="INFRASTRUCTURE",
                    error_message=error_message[:2000],
                )
            )
        return results

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

    def cancel(self, run_id: str) -> None:
        parsed = UUID(run_id)
        with self._state_lock:
            self._canceled_runs.add(parsed)

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
            "adapter": "WebPlaywrightAdapter",
            "runner_version": RUNNER_VERSION,
            "supported_modules": ["login"],
            "supported_operations": sorted(self._engine.SUPPORTED_OPERATIONS),
            "supported_assertions": sorted(self._engine.SUPPORTED_ASSERTIONS),
            "browser_backend": self._browser_backend.health(),
        }

    def _is_canceled(self, run_id: UUID) -> bool:
        with self._state_lock:
            return run_id in self._canceled_runs

    def _terminal_status(self, run_id: UUID, results: list[CaseResult]) -> RunStatus:
        if self._is_canceled(run_id):
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
    def _emit(
        workspace: RunWorkspace,
        reporter: ProgressReporter,
        event: dict[str, object],
    ) -> None:
        workspace.append_event(event)
        try:
            reporter(dict(event))
        except Exception:
            # Progress transport failure cannot rewrite the test outcome. The
            # local JSONL event remains available for later reconciliation.
            pass
