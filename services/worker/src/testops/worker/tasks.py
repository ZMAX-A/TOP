"""Celery tasks that execute immutable Web Runner snapshots."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from testops.contracts import (
    CaseResult,
    CaseResultStatus,
    RunResult,
    RunSnapshot,
    RunStatus,
)
from testops.runners.web import EnvironmentSecretProvider, PlaywrightWebAdapter

from .artifact_store import ArtifactUploader
from .celery_app import celery_app
from .config import WorkerSettings
from .control_plane import ControlPlaneClient

TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.PASSED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELED.value,
        RunStatus.TIMED_OUT.value,
        RunStatus.INFRA_ERROR.value,
    }
)


def _recovery_result(job: RunSnapshot, error: Exception) -> RunResult:
    moment = datetime.now(UTC)
    safe_error = str(error)[:2000]
    return RunResult(
        run_id=job.run_id,
        status=RunStatus.INFRA_ERROR,
        started_at=moment,
        finished_at=moment,
        runner_version="0.1.0",
        case_results=tuple(
            CaseResult(
                case_id=case.case_id,
                case_code=case.case_code,
                status=(CaseResultStatus.INFRA_ERROR if case.enabled else CaseResultStatus.SKIPPED),
                started_at=moment,
                finished_at=moment,
                duration_ms=0,
                failure_category=("WORKER_INFRASTRUCTURE" if case.enabled else "case disabled"),
                error_message=safe_error if case.enabled else None,
            )
            for case in job.cases
        ),
    )


def _canceled_result(job: RunSnapshot) -> RunResult:
    moment = datetime.now(UTC)
    return RunResult(
        run_id=job.run_id,
        status=RunStatus.CANCELED,
        started_at=moment,
        finished_at=moment,
        runner_version="0.1.0",
        case_results=tuple(
            CaseResult(
                case_id=case.case_id,
                case_code=case.case_code,
                status=CaseResultStatus.SKIPPED,
                started_at=moment,
                finished_at=moment,
                duration_ms=0,
                failure_category="run canceled",
            )
            for case in job.cases
        ),
    )


def _existing_result(workspace_root: str, run_id: UUID) -> RunResult | None:
    path = Path(workspace_root) / str(run_id) / "run-result.json"
    if not path.is_file():
        return None
    return RunResult.model_validate(json.loads(path.read_text("utf-8")))


def _upload_artifacts(result: RunResult, settings: WorkerSettings) -> RunResult:
    if not result.artifacts:
        return result
    return ArtifactUploader.from_settings(settings).upload_result(
        result,
        workspace_root=settings.workspace_root,
    )


def _reconcile_events(
    workspace_root: str,
    run_id: UUID,
    control_plane: ControlPlaneClient,
) -> None:
    events_path = Path(workspace_root) / str(run_id) / "events.jsonl"
    if not events_path.is_file():
        return
    with events_path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid Runner event at line {line_number}") from exc
            if not isinstance(event, dict):
                raise RuntimeError(f"invalid Runner event at line {line_number}")
            control_plane.report_event(run_id, event)


@celery_app.task(
    name="testops.execute_run",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
)
def execute_run(snapshot_payload: dict[str, object]) -> dict[str, object]:
    settings = WorkerSettings.from_environment()
    job = RunSnapshot.model_validate(snapshot_payload)
    control_plane = ControlPlaneClient(
        settings.control_plane_url,
        settings.runner_callback_token,
    )
    initial_state = control_plane.run_state(job.run_id)
    if initial_state["status"] in TERMINAL_RUN_STATUSES:
        return {
            "run_id": str(job.run_id),
            "status": str(initial_state["status"]),
            "recovered": False,
            "ignored": True,
        }

    existing = _existing_result(settings.workspace_root, job.run_id)
    if existing is not None:
        _reconcile_events(settings.workspace_root, job.run_id, control_plane)
        uploaded = _upload_artifacts(existing, settings)
        control_plane.report_result(uploaded)
        return {"run_id": str(job.run_id), "status": existing.status.value, "recovered": True}

    if initial_state["cancel_requested"]:
        result = _canceled_result(job)
        control_plane.report_result(result)
        return {
            "run_id": str(job.run_id),
            "status": result.status.value,
            "recovered": False,
        }

    adapter = PlaywrightWebAdapter(
        workspace_root=settings.workspace_root,
        secret_provider=EnvironmentSecretProvider(),
    )
    try:
        control_plane.report_status(
            job.run_id,
            RunStatus.PREPARING,
            worker_key=settings.runner_worker_key,
        )
        adapter.prepare(job)
        control_plane.report_status(
            job.run_id,
            RunStatus.RUNNING,
            worker_key=settings.runner_worker_key,
        )

        def reporter(event: dict[str, object]) -> None:
            try:
                control_plane.report_event(job.run_id, event)
            except Exception:
                # The local JSONL event is replayed before the immutable result
                # callback and again by a retried task if transport stays down.
                pass
            if event.get("event") == "case_started" and control_plane.cancel_requested(job.run_id):
                adapter.cancel(str(job.run_id))

        result = adapter.execute(job, reporter)
    except Exception as exc:
        result = _recovery_result(job, exc)
    _reconcile_events(settings.workspace_root, job.run_id, control_plane)
    uploaded = _upload_artifacts(result, settings)
    control_plane.report_result(uploaded)
    return {"run_id": str(job.run_id), "status": result.status.value, "recovered": False}


@celery_app.task(name="testops.cancel_run")
def cancel_run(run_id: str) -> dict[str, str]:
    # The durable cancellation flag lives in the control plane. An executing
    # task polls it at case boundaries; a queued task will be ignored because
    # its Run has already transitioned to CANCELED.
    UUID(run_id)
    return {"run_id": run_id, "status": "cancel_requested"}
