"""Versioned HTTP routes for the TestOps control plane."""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from testops.contracts import (
    CaseBaseline,
    RunResult,
    RunSnapshot,
    RunStatus,
    RuntimeVariable,
    SecretBinding,
    WebRunConfig,
)

from .artifact_store import ArtifactStoreError, MinioArtifactStore
from .database import get_session
from .identity import CurrentPrincipal, authorize_project, require_system_admin
from .persistence import (
    ArtifactRecord,
    AutomationPackageRecord,
    CaseBaselineRecord,
    EnvironmentRecord,
    RegressionScheduleFiringRecord,
    RegressionScheduleRecord,
    RunCaseRecord,
    RunEventRecord,
    RunnerPoolRecord,
    RunnerWorkerRecord,
    TestRunRecord,
    utc_now,
)
from .quality_services import (
    get_quality_analytics,
    get_quality_policy,
    update_quality_policy,
)
from .quality_webhook_services import (
    acknowledge_quality_alert,
    clear_quality_alert_acknowledgement,
    clear_quality_alert_silence,
    enqueue_quality_webhook_test,
    get_quality_webhook_config,
    list_quality_alert_states,
    list_quality_webhook_deliveries,
    replay_failed_quality_webhook_delivery,
    set_quality_alert_silence,
    update_quality_webhook_config,
)
from .schedule_services import (
    create_regression_schedule,
    list_regression_schedule_firings,
    list_regression_schedules,
    trigger_regression_schedule,
    update_regression_schedule,
)
from .schemas import (
    ArtifactAccessResponse,
    ArtifactResponse,
    AutomationPackageActivateRequest,
    AutomationPackageCreate,
    AutomationPackageDraftCreate,
    AutomationPackageResponse,
    AutomationPackageStatusChangeRequest,
    AutomationPackageValidationRunCreate,
    BaselinePublishRequest,
    BaselineResponse,
    EnvironmentCreate,
    EnvironmentResponse,
    EnvironmentUpdate,
    ExecutionPolicyResponse,
    ExecutionPolicyUpdate,
    InternalRunEventCreate,
    InternalRunResultResponse,
    InternalRunStatusResponse,
    InternalRunStatusUpdate,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
    QualityAlertAcknowledgementUpdate,
    QualityAlertMetric,
    QualityAlertSilenceUpdate,
    QualityAlertStateResponse,
    QualityAnalyticsResponse,
    QualityPolicyResponse,
    QualityPolicyUpdate,
    QualityWebhookConfigResponse,
    QualityWebhookConfigUpdate,
    QualityWebhookDeliveryResponse,
    QualityWebhookReplayRequest,
    RegressionScheduleCreate,
    RegressionScheduleFiringResponse,
    RegressionScheduleResponse,
    RegressionScheduleUpdate,
    RunBatchCancelRequest,
    RunBatchCancelResponse,
    RunCaseResponse,
    RunCreate,
    RunDetailResponse,
    RunEventResponse,
    RunListResponse,
    RunnerPoolCatalogResponse,
    RunnerPoolCreate,
    RunnerPoolResponse,
    RunnerPoolUpdate,
    RunnerWorkerHeartbeat,
    RunnerWorkerResponse,
    RunnerWorkerUpdate,
    RunRerunRequest,
    RunResponse,
    TargetCreate,
    TargetResponse,
    TargetUpdate,
)
from .services import (
    TERMINAL_RUN_STATUSES,
    activate_automation_package,
    audit_artifact_access,
    cancel_run,
    cancel_runs,
    create_automation_package,
    create_automation_package_validation_run,
    create_environment,
    create_project,
    create_rerun,
    create_run,
    create_runner_pool,
    create_target,
    deprecate_automation_package,
    get_automation_package,
    get_baseline,
    get_execution_policy,
    get_run,
    get_run_artifact,
    heartbeat_runner_worker,
    list_automation_packages,
    list_baselines,
    list_environments,
    list_projects,
    list_run_artifacts,
    list_run_cases,
    list_run_events,
    list_runner_pools,
    list_runner_workers,
    list_runs,
    list_targets,
    publish_baseline,
    record_run_event,
    record_run_result,
    revoke_automation_package,
    update_environment,
    update_execution_policy,
    update_project,
    update_run_status,
    update_runner_pool,
    update_runner_worker,
    update_target,
)

router = APIRouter(prefix="/api/v1")
Session = Annotated[AsyncSession, Depends(get_session)]


def require_runner_token(
    request: Request,
    token: Annotated[str | None, Header(alias="X-Runner-Token")] = None,
) -> None:
    configured = request.app.state.settings.runner_callback_token
    if not configured:
        raise HTTPException(status_code=503, detail="Runner callback token is not configured")
    if token is None or not hmac.compare_digest(token, configured):
        raise HTTPException(status_code=401, detail="invalid Runner callback token")


RunnerAuth = Annotated[None, Depends(require_runner_token)]


def _environment_response(record: EnvironmentRecord) -> EnvironmentResponse:
    document = record.config_document
    raw_web_config = document.get("web_config")
    return EnvironmentResponse(
        id=record.id,
        project_id=record.project_id,
        target_id=record.target_id,
        runner_pool_id=record.runner_pool_id,
        key=record.key,
        name=record.name,
        web_config=WebRunConfig.model_validate(raw_web_config) if raw_web_config else None,
        variables=tuple(RuntimeVariable.model_validate(item) for item in document["variables"]),
        secret_bindings=tuple(
            SecretBinding.model_validate(item) for item in document["secret_bindings"]
        ),
        config_hash=record.config_hash,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _runner_worker_response(
    record: RunnerWorkerRecord,
    pool: RunnerPoolRecord,
    *,
    heartbeat_ttl_seconds: int,
) -> RunnerWorkerResponse:
    last_heartbeat_at = record.last_heartbeat_at
    if last_heartbeat_at.tzinfo is None:
        last_heartbeat_at = last_heartbeat_at.replace(tzinfo=utc_now().tzinfo)
    health = (
        "ONLINE"
        if last_heartbeat_at >= utc_now() - timedelta(seconds=heartbeat_ttl_seconds)
        else "STALE"
    )
    return RunnerWorkerResponse(
        id=record.id,
        pool_id=record.pool_id,
        pool_key=pool.key,
        worker_key=record.worker_key,
        display_name=record.display_name,
        runner_version=record.runner_version,
        max_slots=record.max_slots,
        capabilities=record.capabilities,
        status=record.status,
        health=health,
        last_heartbeat_at=record.last_heartbeat_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _baseline_response(record: CaseBaselineRecord) -> BaselineResponse:
    return BaselineResponse.model_validate(record)


def _package_response(record: AutomationPackageRecord) -> AutomationPackageResponse:
    return AutomationPackageResponse.model_validate(record)


def _regression_schedule_response(
    record: RegressionScheduleRecord,
) -> RegressionScheduleResponse:
    return RegressionScheduleResponse.model_validate(record)


def _regression_firing_response(
    record: RegressionScheduleFiringRecord,
) -> RegressionScheduleFiringResponse:
    return RegressionScheduleFiringResponse.model_validate(record)


def _run_response(record: TestRunRecord) -> RunResponse:
    return RunResponse.model_validate(record)


def _run_case_response(record: RunCaseRecord) -> RunCaseResponse:
    return RunCaseResponse.model_validate(record)


def _artifact_response(record: ArtifactRecord) -> ArtifactResponse:
    return ArtifactResponse.model_validate(record)


def _event_response(record: RunEventRecord) -> RunEventResponse:
    return RunEventResponse.model_validate(record)


async def _run_detail_response(
    session: AsyncSession,
    record: TestRunRecord,
) -> RunDetailResponse:
    summary = _run_response(record).model_dump()
    cases = await list_run_cases(session, record.id)
    artifacts = await list_run_artifacts(session, record.id)
    return RunDetailResponse(
        **summary,
        snapshot=RunSnapshot.model_validate(record.snapshot),
        result=RunResult.model_validate(record.result_document) if record.result_document else None,
        cases=tuple(_run_case_response(item) for item in cases),
        artifacts=tuple(_artifact_response(item) for item in artifacts),
    )


def _sse_event(record: RunEventRecord) -> str:
    payload = json.dumps(
        _event_response(record).model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {record.sequence}\nevent: {record.event_type}\ndata: {payload}\n\n"


@router.get(
    "/runner-pools/catalog",
    response_model=tuple[RunnerPoolCatalogResponse, ...],
    tags=["execution-capacity"],
)
async def get_runner_pool_catalog(
    _principal: CurrentPrincipal,
    request: Request,
    session: Session,
) -> object:
    pools = await list_runner_pools(
        session,
        heartbeat_ttl_seconds=request.app.state.settings.runner_heartbeat_ttl_seconds,
        active_only=True,
    )
    return tuple(
        {
            "id": pool["id"],
            "key": pool["key"],
            "name": pool["name"],
            "target_types": pool["target_types"],
            "status": pool["status"],
            "available_slots": pool["available_slots"],
        }
        for pool in pools
    )


@router.get(
    "/admin/runner-pools",
    response_model=tuple[RunnerPoolResponse, ...],
    tags=["system-management"],
)
async def get_admin_runner_pools(
    principal: CurrentPrincipal,
    request: Request,
    session: Session,
) -> object:
    require_system_admin(principal)
    return await list_runner_pools(
        session,
        heartbeat_ttl_seconds=request.app.state.settings.runner_heartbeat_ttl_seconds,
    )


@router.post(
    "/admin/runner-pools",
    response_model=RunnerPoolResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["system-management"],
)
async def post_admin_runner_pool(
    payload: RunnerPoolCreate,
    principal: CurrentPrincipal,
    request: Request,
    session: Session,
) -> object:
    require_system_admin(principal)
    return await create_runner_pool(
        session,
        payload,
        principal.user_id,
        heartbeat_ttl_seconds=request.app.state.settings.runner_heartbeat_ttl_seconds,
    )


@router.patch(
    "/admin/runner-pools/{pool_id}",
    response_model=RunnerPoolResponse,
    tags=["system-management"],
)
async def patch_admin_runner_pool(
    pool_id: UUID,
    payload: RunnerPoolUpdate,
    principal: CurrentPrincipal,
    request: Request,
    session: Session,
) -> object:
    require_system_admin(principal)
    return await update_runner_pool(
        session,
        pool_id,
        payload,
        principal.user_id,
        heartbeat_ttl_seconds=request.app.state.settings.runner_heartbeat_ttl_seconds,
    )


@router.get(
    "/admin/runner-workers",
    response_model=tuple[RunnerWorkerResponse, ...],
    tags=["system-management"],
)
async def get_admin_runner_workers(
    principal: CurrentPrincipal,
    request: Request,
    session: Session,
    pool_id: UUID | None = None,
) -> tuple[RunnerWorkerResponse, ...]:
    require_system_admin(principal)
    rows = await list_runner_workers(session, pool_id=pool_id)
    return tuple(
        _runner_worker_response(
            worker,
            pool,
            heartbeat_ttl_seconds=request.app.state.settings.runner_heartbeat_ttl_seconds,
        )
        for worker, pool in rows
    )


@router.patch(
    "/admin/runner-workers/{worker_id}",
    response_model=RunnerWorkerResponse,
    tags=["system-management"],
)
async def patch_admin_runner_worker(
    worker_id: UUID,
    payload: RunnerWorkerUpdate,
    principal: CurrentPrincipal,
    request: Request,
    session: Session,
) -> RunnerWorkerResponse:
    require_system_admin(principal)
    worker, pool = await update_runner_worker(session, worker_id, payload, principal.user_id)
    return _runner_worker_response(
        worker,
        pool,
        heartbeat_ttl_seconds=request.app.state.settings.runner_heartbeat_ttl_seconds,
    )


@router.put(
    "/internal/runner-workers/{worker_key}/heartbeat",
    response_model=RunnerWorkerResponse,
    tags=["runner-internal"],
)
async def put_internal_runner_worker_heartbeat(
    worker_key: Annotated[str, Path(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{2,127}$")],
    payload: RunnerWorkerHeartbeat,
    request: Request,
    session: Session,
    _runner_auth: RunnerAuth,
) -> RunnerWorkerResponse:
    worker, pool = await heartbeat_runner_worker(session, worker_key, payload)
    return _runner_worker_response(
        worker,
        pool,
        heartbeat_ttl_seconds=request.app.state.settings.runner_heartbeat_ttl_seconds,
    )


@router.post(
    "/projects",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["projects"],
)
async def post_project(
    payload: ProjectCreate,
    principal: CurrentPrincipal,
    session: Session,
) -> object:
    require_system_admin(principal)
    return await create_project(session, payload, principal.user_id)


@router.get("/projects", response_model=tuple[ProjectResponse, ...], tags=["projects"])
async def get_projects(principal: CurrentPrincipal, session: Session) -> object:
    return await list_projects(
        session,
        user_id=principal.user_id,
        is_system_admin=principal.is_system_admin,
    )


@router.patch(
    "/projects/{project_id}",
    response_model=ProjectResponse,
    tags=["projects"],
)
async def patch_project(
    project_id: UUID,
    payload: ProjectUpdate,
    principal: CurrentPrincipal,
    session: Session,
) -> object:
    require_system_admin(principal)
    return await update_project(session, project_id, payload, principal.user_id)


@router.get(
    "/projects/{project_id}/execution-policy",
    response_model=ExecutionPolicyResponse,
    tags=["execution-capacity"],
)
async def get_project_execution_policy(
    project_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> object:
    await authorize_project(session, principal, project_id, "project:read")
    return await get_execution_policy(session, project_id)


@router.patch(
    "/projects/{project_id}/execution-policy",
    response_model=ExecutionPolicyResponse,
    tags=["execution-capacity"],
)
async def patch_project_execution_policy(
    project_id: UUID,
    payload: ExecutionPolicyUpdate,
    principal: CurrentPrincipal,
    session: Session,
) -> object:
    await authorize_project(session, principal, project_id, "project:manage")
    return await update_execution_policy(session, project_id, payload, principal.user_id)


@router.get(
    "/projects/{project_id}/quality-policy",
    response_model=QualityPolicyResponse,
    tags=["quality-analytics"],
)
async def get_project_quality_policy(
    project_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> QualityPolicyResponse:
    await authorize_project(session, principal, project_id, "project:read")
    return await get_quality_policy(session, project_id)


@router.patch(
    "/projects/{project_id}/quality-policy",
    response_model=QualityPolicyResponse,
    tags=["quality-analytics"],
)
async def patch_project_quality_policy(
    project_id: UUID,
    payload: QualityPolicyUpdate,
    principal: CurrentPrincipal,
    session: Session,
) -> QualityPolicyResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    return await update_quality_policy(session, project_id, payload, principal.user_id)


@router.get(
    "/projects/{project_id}/quality/webhook",
    response_model=QualityWebhookConfigResponse,
    tags=["quality-analytics"],
)
async def get_project_quality_webhook(
    project_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> QualityWebhookConfigResponse:
    await authorize_project(session, principal, project_id, "project:read")
    return await get_quality_webhook_config(session, project_id)


@router.patch(
    "/projects/{project_id}/quality/webhook",
    response_model=QualityWebhookConfigResponse,
    tags=["quality-analytics"],
)
async def patch_project_quality_webhook(
    project_id: UUID,
    payload: QualityWebhookConfigUpdate,
    principal: CurrentPrincipal,
    session: Session,
) -> QualityWebhookConfigResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    return await update_quality_webhook_config(session, project_id, payload, principal.user_id)


@router.put(
    "/projects/{project_id}/quality/webhook/silence",
    response_model=QualityWebhookConfigResponse,
    tags=["quality-analytics"],
)
async def put_project_quality_alert_silence(
    project_id: UUID,
    payload: QualityAlertSilenceUpdate,
    principal: CurrentPrincipal,
    session: Session,
) -> QualityWebhookConfigResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    return await set_quality_alert_silence(
        session,
        project_id,
        payload,
        principal.user_id,
    )


@router.delete(
    "/projects/{project_id}/quality/webhook/silence",
    response_model=QualityWebhookConfigResponse,
    tags=["quality-analytics"],
)
async def delete_project_quality_alert_silence(
    project_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> QualityWebhookConfigResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    return await clear_quality_alert_silence(session, project_id, principal.user_id)


@router.post(
    "/projects/{project_id}/quality/webhook/test",
    response_model=QualityWebhookDeliveryResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["quality-analytics"],
)
async def test_project_quality_webhook(
    project_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> QualityWebhookDeliveryResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    return await enqueue_quality_webhook_test(session, project_id, principal.user_id)


@router.get(
    "/projects/{project_id}/quality/webhook/deliveries",
    response_model=list[QualityWebhookDeliveryResponse],
    tags=["quality-analytics"],
)
async def get_project_quality_webhook_deliveries(
    project_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> tuple[QualityWebhookDeliveryResponse, ...]:
    await authorize_project(session, principal, project_id, "project:read")
    return await list_quality_webhook_deliveries(session, project_id, limit=limit)


@router.post(
    "/projects/{project_id}/quality/webhook/deliveries/{delivery_id}/replay",
    response_model=QualityWebhookDeliveryResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["quality-analytics"],
)
async def replay_project_quality_webhook_delivery(
    project_id: UUID,
    delivery_id: UUID,
    payload: QualityWebhookReplayRequest,
    principal: CurrentPrincipal,
    session: Session,
) -> QualityWebhookDeliveryResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    return await replay_failed_quality_webhook_delivery(
        session,
        project_id,
        delivery_id,
        payload,
        principal.user_id,
    )


@router.get(
    "/projects/{project_id}/quality/webhook/states",
    response_model=list[QualityAlertStateResponse],
    tags=["quality-analytics"],
)
async def get_project_quality_alert_states(
    project_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> tuple[QualityAlertStateResponse, ...]:
    await authorize_project(session, principal, project_id, "project:read")
    return await list_quality_alert_states(session, project_id)


@router.put(
    "/projects/{project_id}/quality/webhook/states/{metric}/acknowledgement",
    response_model=QualityAlertStateResponse,
    tags=["quality-analytics"],
)
async def put_project_quality_alert_acknowledgement(
    project_id: UUID,
    metric: QualityAlertMetric,
    payload: QualityAlertAcknowledgementUpdate,
    principal: CurrentPrincipal,
    session: Session,
) -> QualityAlertStateResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    return await acknowledge_quality_alert(
        session,
        project_id,
        metric,
        payload,
        principal.user_id,
    )


@router.delete(
    "/projects/{project_id}/quality/webhook/states/{metric}/acknowledgement",
    response_model=QualityAlertStateResponse,
    tags=["quality-analytics"],
)
async def delete_project_quality_alert_acknowledgement(
    project_id: UUID,
    metric: QualityAlertMetric,
    principal: CurrentPrincipal,
    session: Session,
) -> QualityAlertStateResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    return await clear_quality_alert_acknowledgement(
        session,
        project_id,
        metric,
        principal.user_id,
    )


@router.get(
    "/projects/{project_id}/quality/analytics",
    response_model=QualityAnalyticsResponse,
    tags=["quality-analytics"],
)
async def get_project_quality_analytics(
    project_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
    window_days: Annotated[int | None, Query(ge=1, le=90)] = None,
    target_id: UUID | None = None,
    environment_id: UUID | None = None,
    baseline_id: UUID | None = None,
) -> QualityAnalyticsResponse:
    await authorize_project(session, principal, project_id, "run:read")
    return await get_quality_analytics(
        session,
        project_id,
        window_days=window_days,
        target_id=target_id,
        environment_id=environment_id,
        baseline_id=baseline_id,
    )


@router.post(
    "/projects/{project_id}/regression-schedules",
    response_model=RegressionScheduleResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["regression-schedules"],
)
async def post_regression_schedule(
    project_id: UUID,
    payload: RegressionScheduleCreate,
    principal: CurrentPrincipal,
    session: Session,
) -> RegressionScheduleResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    record = await create_regression_schedule(session, project_id, payload, principal.user_id)
    return _regression_schedule_response(record)


@router.get(
    "/projects/{project_id}/regression-schedules",
    response_model=tuple[RegressionScheduleResponse, ...],
    tags=["regression-schedules"],
)
async def get_regression_schedules(
    project_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> tuple[RegressionScheduleResponse, ...]:
    await authorize_project(session, principal, project_id, "project:read")
    records = await list_regression_schedules(session, project_id)
    return tuple(_regression_schedule_response(record) for record in records)


@router.patch(
    "/projects/{project_id}/regression-schedules/{schedule_id}",
    response_model=RegressionScheduleResponse,
    tags=["regression-schedules"],
)
async def patch_regression_schedule(
    project_id: UUID,
    schedule_id: UUID,
    payload: RegressionScheduleUpdate,
    principal: CurrentPrincipal,
    session: Session,
) -> RegressionScheduleResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    record = await update_regression_schedule(
        session,
        project_id,
        schedule_id,
        payload,
        principal.user_id,
    )
    return _regression_schedule_response(record)


@router.get(
    "/projects/{project_id}/regression-schedules/{schedule_id}/firings",
    response_model=tuple[RegressionScheduleFiringResponse, ...],
    tags=["regression-schedules"],
)
async def get_regression_schedule_firings(
    project_id: UUID,
    schedule_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> tuple[RegressionScheduleFiringResponse, ...]:
    await authorize_project(session, principal, project_id, "project:read")
    records = await list_regression_schedule_firings(
        session,
        project_id,
        schedule_id,
        limit=limit,
    )
    return tuple(_regression_firing_response(record) for record in records)


@router.post(
    "/projects/{project_id}/regression-schedules/{schedule_id}/trigger",
    response_model=RunResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["regression-schedules"],
)
async def post_regression_schedule_trigger(
    project_id: UUID,
    schedule_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ],
) -> RunResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    record, created = await trigger_regression_schedule(
        session,
        project_id,
        schedule_id,
        idempotency_key=idempotency_key,
        actor_id=principal.user_id,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return _run_response(record)


@router.post(
    "/projects/{project_id}/targets",
    response_model=TargetResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["targets"],
)
async def post_target(
    project_id: UUID,
    payload: TargetCreate,
    principal: CurrentPrincipal,
    session: Session,
) -> object:
    await authorize_project(session, principal, project_id, "project:manage")
    return await create_target(session, project_id, payload, principal.user_id)


@router.get(
    "/projects/{project_id}/targets",
    response_model=tuple[TargetResponse, ...],
    tags=["targets"],
)
async def get_targets(
    project_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> object:
    await authorize_project(session, principal, project_id, "project:read")
    return await list_targets(session, project_id)


@router.patch(
    "/projects/{project_id}/targets/{target_id}",
    response_model=TargetResponse,
    tags=["targets"],
)
async def patch_target(
    project_id: UUID,
    target_id: UUID,
    payload: TargetUpdate,
    principal: CurrentPrincipal,
    session: Session,
) -> object:
    await authorize_project(session, principal, project_id, "project:manage")
    return await update_target(session, project_id, target_id, payload, principal.user_id)


@router.post(
    "/projects/{project_id}/targets/{target_id}/environments",
    response_model=EnvironmentResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["environments"],
)
async def post_environment(
    project_id: UUID,
    target_id: UUID,
    payload: EnvironmentCreate,
    principal: CurrentPrincipal,
    session: Session,
) -> EnvironmentResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    record = await create_environment(session, project_id, target_id, payload, principal.user_id)
    return _environment_response(record)


@router.get(
    "/projects/{project_id}/targets/{target_id}/environments",
    response_model=tuple[EnvironmentResponse, ...],
    tags=["environments"],
)
async def get_environments(
    project_id: UUID,
    target_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> tuple[EnvironmentResponse, ...]:
    await authorize_project(session, principal, project_id, "project:read")
    records = await list_environments(session, project_id, target_id)
    return tuple(_environment_response(record) for record in records)


@router.patch(
    "/projects/{project_id}/targets/{target_id}/environments/{environment_id}",
    response_model=EnvironmentResponse,
    tags=["environments"],
)
async def patch_environment(
    project_id: UUID,
    target_id: UUID,
    environment_id: UUID,
    payload: EnvironmentUpdate,
    principal: CurrentPrincipal,
    session: Session,
) -> EnvironmentResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    record = await update_environment(
        session,
        project_id,
        target_id,
        environment_id,
        payload,
        principal.user_id,
    )
    return _environment_response(record)


@router.post(
    "/projects/{project_id}/baselines",
    response_model=BaselineResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["baselines"],
)
async def post_baseline(
    project_id: UUID,
    payload: BaselinePublishRequest,
    principal: CurrentPrincipal,
    session: Session,
    response: Response,
) -> BaselineResponse:
    # Direct publication is reserved for initial/legacy baseline registration.
    # Subsequent changes must use the governed Change Request workflow.
    require_system_admin(principal)
    await authorize_project(session, principal, project_id, "baseline:publish")
    record, created = await publish_baseline(session, project_id, payload, principal.user_id)
    if not created:
        response.status_code = status.HTTP_200_OK
    return _baseline_response(record)


@router.get(
    "/projects/{project_id}/baselines",
    response_model=tuple[BaselineResponse, ...],
    tags=["baselines"],
)
async def get_baselines(
    project_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> tuple[BaselineResponse, ...]:
    await authorize_project(session, principal, project_id, "baseline:read")
    records = await list_baselines(session, project_id)
    return tuple(_baseline_response(record) for record in records)


@router.get(
    "/projects/{project_id}/baselines/{baseline_id}",
    response_model=CaseBaseline,
    tags=["baselines"],
)
async def get_baseline_document(
    project_id: UUID,
    baseline_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> CaseBaseline:
    await authorize_project(session, principal, project_id, "baseline:read")
    record = await get_baseline(session, project_id, baseline_id)
    return CaseBaseline.model_validate(record.document)


@router.post(
    "/projects/{project_id}/targets/{target_id}/automation-packages",
    response_model=AutomationPackageResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["automation-packages"],
)
async def post_automation_package(
    project_id: UUID,
    target_id: UUID,
    payload: AutomationPackageCreate,
    principal: CurrentPrincipal,
    session: Session,
) -> AutomationPackageResponse:
    # Backward-compatible ACTIVE registration is reserved for initial/legacy imports.
    # Normal package releases must use the governed draft/validation/activation flow.
    require_system_admin(principal)
    await authorize_project(session, principal, project_id, "project:manage")
    record = await create_automation_package(
        session, project_id, target_id, payload, principal.user_id
    )
    return _package_response(record)


@router.post(
    "/projects/{project_id}/targets/{target_id}/automation-packages/drafts",
    response_model=AutomationPackageResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["automation-packages"],
)
async def post_automation_package_draft(
    project_id: UUID,
    target_id: UUID,
    payload: AutomationPackageDraftCreate,
    principal: CurrentPrincipal,
    session: Session,
) -> AutomationPackageResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    record = await create_automation_package(
        session,
        project_id,
        target_id,
        payload,
        principal.user_id,
        status="DRAFT",
    )
    return _package_response(record)


@router.get(
    "/projects/{project_id}/targets/{target_id}/automation-packages",
    response_model=tuple[AutomationPackageResponse, ...],
    tags=["automation-packages"],
)
async def get_automation_packages(
    project_id: UUID,
    target_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> tuple[AutomationPackageResponse, ...]:
    await authorize_project(session, principal, project_id, "project:read")
    records = await list_automation_packages(session, project_id, target_id)
    return tuple(_package_response(record) for record in records)


@router.get(
    "/projects/{project_id}/targets/{target_id}/automation-packages/{package_id}",
    response_model=AutomationPackageResponse,
    tags=["automation-packages"],
)
async def get_automation_package_detail(
    project_id: UUID,
    target_id: UUID,
    package_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> AutomationPackageResponse:
    await authorize_project(session, principal, project_id, "project:read")
    record = await get_automation_package(session, project_id, target_id, package_id)
    return _package_response(record)


@router.post(
    "/projects/{project_id}/targets/{target_id}/automation-packages/{package_id}/validation-runs",
    response_model=RunResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["automation-packages"],
)
async def post_automation_package_validation_run(
    project_id: UUID,
    target_id: UUID,
    package_id: UUID,
    payload: AutomationPackageValidationRunCreate,
    principal: CurrentPrincipal,
    session: Session,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ],
) -> RunResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    record, created = await create_automation_package_validation_run(
        session,
        project_id,
        target_id,
        package_id,
        payload,
        idempotency_key=idempotency_key,
        actor_id=principal.user_id,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return _run_response(record)


@router.post(
    "/projects/{project_id}/targets/{target_id}/automation-packages/{package_id}/activate",
    response_model=AutomationPackageResponse,
    tags=["automation-packages"],
)
async def post_automation_package_activation(
    project_id: UUID,
    target_id: UUID,
    package_id: UUID,
    payload: AutomationPackageActivateRequest,
    principal: CurrentPrincipal,
    session: Session,
) -> AutomationPackageResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    record = await activate_automation_package(
        session,
        project_id,
        target_id,
        package_id,
        payload,
        principal.user_id,
    )
    return _package_response(record)


@router.post(
    "/projects/{project_id}/targets/{target_id}/automation-packages/{package_id}/deprecate",
    response_model=AutomationPackageResponse,
    tags=["automation-packages"],
)
async def post_automation_package_deprecation(
    project_id: UUID,
    target_id: UUID,
    package_id: UUID,
    payload: AutomationPackageStatusChangeRequest,
    principal: CurrentPrincipal,
    session: Session,
) -> AutomationPackageResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    record = await deprecate_automation_package(
        session,
        project_id,
        target_id,
        package_id,
        payload,
        principal.user_id,
    )
    return _package_response(record)


@router.post(
    "/projects/{project_id}/targets/{target_id}/automation-packages/{package_id}/revoke",
    response_model=AutomationPackageResponse,
    tags=["automation-packages"],
)
async def post_automation_package_revocation(
    project_id: UUID,
    target_id: UUID,
    package_id: UUID,
    payload: AutomationPackageStatusChangeRequest,
    principal: CurrentPrincipal,
    session: Session,
) -> AutomationPackageResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    record = await revoke_automation_package(
        session,
        project_id,
        target_id,
        package_id,
        payload,
        principal.user_id,
    )
    return _package_response(record)


@router.post(
    "/runs",
    response_model=RunResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["runs"],
)
async def post_run(
    payload: RunCreate,
    principal: CurrentPrincipal,
    session: Session,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ],
) -> RunResponse:
    await authorize_project(session, principal, payload.project_id, "run:create")
    record, created = await create_run(
        session,
        payload,
        idempotency_key=idempotency_key,
        actor_id=principal.user_id,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return _run_response(record)


@router.get("/runs/{run_id}", response_model=RunDetailResponse, tags=["runs"])
async def get_run_detail(
    run_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> RunDetailResponse:
    record = await get_run(session, run_id)
    await authorize_project(session, principal, record.project_id, "run:read")
    return await _run_detail_response(session, record)


@router.get(
    "/runs/{run_id}/events",
    response_model=tuple[RunEventResponse, ...],
    tags=["run-events"],
)
async def get_run_events(
    run_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> tuple[RunEventResponse, ...]:
    record = await get_run(session, run_id)
    await authorize_project(session, principal, record.project_id, "run:read")
    events = await list_run_events(
        session,
        run_id,
        after_sequence=after_sequence,
        limit=limit,
    )
    return tuple(_event_response(event) for event in events)


@router.get("/runs/{run_id}/events/stream", tags=["run-events"])
async def stream_run_events(
    run_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    session: Session,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    follow: bool = True,
    last_event_id: Annotated[int | None, Header(alias="Last-Event-ID", ge=0)] = None,
) -> StreamingResponse:
    record = await get_run(session, run_id)
    await authorize_project(session, principal, record.project_id, "run:read")
    # The dependency session otherwise remains in a read transaction for the
    # lifetime of the stream and can pin a database connection indefinitely.
    await session.rollback()
    initial_sequence = max(after_sequence, last_event_id or 0)
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory

    async def events() -> AsyncIterator[str]:
        current_sequence = initial_sequence
        last_heartbeat = time.monotonic()
        while not await request.is_disconnected():
            async with session_factory() as event_session:
                batch = await list_run_events(
                    event_session,
                    run_id,
                    after_sequence=current_sequence,
                    limit=200,
                )
                current_run = await get_run(event_session, run_id)
            for event in batch:
                current_sequence = event.sequence
                yield _sse_event(event)
            if not follow:
                break
            if not batch and RunStatus(current_run.status) in TERMINAL_RUN_STATUSES:
                break
            moment = time.monotonic()
            if moment - last_heartbeat >= settings.run_event_heartbeat_seconds:
                yield ": keep-alive\n\n"
                last_heartbeat = moment
            await asyncio.sleep(settings.run_event_poll_seconds)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/runs/{run_id}/artifacts/{artifact_id}/access",
    response_model=ArtifactAccessResponse,
    tags=["run-artifacts"],
)
async def get_artifact_access(
    run_id: UUID,
    artifact_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    session: Session,
) -> ArtifactAccessResponse:
    run = await get_run(session, run_id)
    await authorize_project(session, principal, run.project_id, "run:read")
    artifact = await get_run_artifact(session, run_id, artifact_id)
    store: MinioArtifactStore | None = request.app.state.artifact_store
    if store is None:
        raise HTTPException(status_code=503, detail="artifact object storage is not configured")
    try:
        access = store.create_download_access(
            artifact.uri,
            run_id=run_id,
            filename=artifact.name,
        )
    except ArtifactStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await audit_artifact_access(
        session,
        artifact,
        project_id=run.project_id,
        actor_id=principal.user_id,
        expires_in_seconds=access.expires_in_seconds,
    )
    return ArtifactAccessResponse(
        artifact_id=artifact.artifact_id,
        name=artifact.name,
        kind=artifact.kind,
        digest=artifact.digest,
        size_bytes=artifact.size_bytes,
        url=access.url,
        expires_at=utc_now() + timedelta(seconds=access.expires_in_seconds),
    )


@router.get(
    "/projects/{project_id}/runs",
    response_model=RunListResponse,
    tags=["runs"],
)
async def get_project_runs(
    project_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
    run_statuses: Annotated[list[RunStatus] | None, Query(alias="status")] = None,
    target_id: UUID | None = None,
    environment_id: UUID | None = None,
    created_by: UUID | None = None,
    source_run_id: UUID | None = None,
    case_code: Annotated[str | None, Query(pattern=r"^TC-[A-Z0-9-]+$")] = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunListResponse:
    await authorize_project(session, principal, project_id, "run:read")
    records, total = await list_runs(
        session,
        project_id,
        statuses=tuple(run_statuses or ()),
        target_id=target_id,
        environment_id=environment_id,
        created_by=created_by,
        source_run_id=source_run_id,
        case_code=case_code,
        created_from=created_from,
        created_to=created_to,
        limit=limit,
        offset=offset,
    )
    return RunListResponse(items=tuple(_run_response(record) for record in records), total=total)


@router.post(
    "/projects/{project_id}/runs/batch-cancel",
    response_model=RunBatchCancelResponse,
    tags=["runs"],
)
async def post_project_runs_batch_cancel(
    project_id: UUID,
    payload: RunBatchCancelRequest,
    principal: CurrentPrincipal,
    session: Session,
) -> RunBatchCancelResponse:
    await authorize_project(session, principal, project_id, "run:cancel")
    records, changed = await cancel_runs(
        session,
        project_id,
        payload.run_ids,
        principal.user_id,
    )
    return RunBatchCancelResponse(
        items=tuple(_run_response(record) for record in records),
        requested=len(payload.run_ids),
        changed=changed,
    )


@router.post("/runs/{run_id}/cancel", response_model=RunResponse, tags=["runs"])
async def post_run_cancel(
    run_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> RunResponse:
    record = await get_run(session, run_id)
    await authorize_project(session, principal, record.project_id, "run:cancel")
    return _run_response(await cancel_run(session, run_id, principal.user_id))


@router.post(
    "/runs/{run_id}/rerun",
    response_model=RunResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["runs"],
)
async def post_run_rerun(
    run_id: UUID,
    payload: RunRerunRequest,
    principal: CurrentPrincipal,
    session: Session,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ],
) -> RunResponse:
    source = await get_run(session, run_id)
    await authorize_project(session, principal, source.project_id, "run:create")
    record, created = await create_rerun(
        session,
        run_id,
        mode=payload.mode,
        idempotency_key=idempotency_key,
        actor_id=principal.user_id,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return _run_response(record)


@router.get(
    "/internal/runs/{run_id}",
    response_model=RunResponse,
    tags=["runner-internal"],
)
async def get_internal_run_state(
    run_id: UUID,
    _auth: RunnerAuth,
    session: Session,
) -> RunResponse:
    return _run_response(await get_run(session, run_id))


@router.post(
    "/internal/runs/{run_id}/status",
    response_model=InternalRunStatusResponse,
    tags=["runner-internal"],
)
async def post_internal_run_status(
    run_id: UUID,
    payload: InternalRunStatusUpdate,
    _auth: RunnerAuth,
    session: Session,
) -> InternalRunStatusResponse:
    record, changed = await update_run_status(
        session,
        run_id,
        payload.status,
        worker_key=payload.worker_key,
    )
    return InternalRunStatusResponse(
        run_id=record.id,
        status=RunStatus(record.status),
        changed=changed,
    )


@router.post(
    "/internal/runs/{run_id}/events",
    response_model=RunEventResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["runner-internal"],
)
async def post_internal_run_event(
    run_id: UUID,
    payload: InternalRunEventCreate,
    _auth: RunnerAuth,
    session: Session,
    response: Response,
) -> RunEventResponse:
    event, created = await record_run_event(session, run_id, payload)
    if not created:
        response.status_code = status.HTTP_200_OK
    return _event_response(event)


@router.post(
    "/internal/runs/{run_id}/result",
    response_model=InternalRunResultResponse,
    tags=["runner-internal"],
)
async def post_internal_run_result(
    run_id: UUID,
    payload: RunResult,
    _auth: RunnerAuth,
    session: Session,
) -> InternalRunResultResponse:
    record, created = await record_run_result(session, run_id, payload)
    assert record.result_digest is not None
    return InternalRunResultResponse(
        run_id=record.id,
        status=RunStatus(record.status),
        result_digest=record.result_digest,
        created=created,
    )
