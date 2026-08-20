"""Transactional control-plane services for projects and immutable Runs."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from testops.contracts import (
    AutomationPackageRef,
    CaseBaseline,
    CaseBaselineRef,
    CaseResultStatus,
    RunResult,
    RunSnapshot,
    RunStatus,
    RuntimeVariable,
    SecretBinding,
    TargetType,
    WebRunConfig,
    canonical_sha256,
)

from .persistence import (
    ArtifactRecord,
    AuditLogRecord,
    AutomationPackageRecord,
    CaseBaselineRecord,
    ChangeRequestRecord,
    DispatchOutboxRecord,
    EnvironmentRecord,
    ProjectMemberRecord,
    ProjectRecord,
    RegressionScheduleRecord,
    RunCaseRecord,
    RunEventRecord,
    RunnerPoolRecord,
    RunnerSlotLeaseRecord,
    RunnerWorkerRecord,
    TestRunRecord,
    TestTargetRecord,
    utc_now,
)
from .schemas import (
    AutomationPackageActivateRequest,
    AutomationPackageCreate,
    AutomationPackageDraftCreate,
    AutomationPackageStatusChangeRequest,
    AutomationPackageValidationRunCreate,
    BaselinePublishRequest,
    EnvironmentCreate,
    EnvironmentUpdate,
    ExecutionPolicyUpdate,
    InternalRunEventCreate,
    ProjectCreate,
    ProjectUpdate,
    RunCreate,
    RunnerPoolCreate,
    RunnerPoolUpdate,
    RunnerWorkerHeartbeat,
    RunnerWorkerUpdate,
    TargetCreate,
    TargetUpdate,
)
from .state_machine import InvalidRunTransition, require_run_transition

RUNNER_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.PASSED,
        RunStatus.FAILED,
        RunStatus.CANCELED,
        RunStatus.TIMED_OUT,
        RunStatus.INFRA_ERROR,
    }
)
IN_FLIGHT_RUN_STATUSES = frozenset(
    {
        RunStatus.QUEUED,
        RunStatus.PREPARING,
        RunStatus.RUNNING,
    }
)


class ServiceError(RuntimeError):
    status_code = 500


class ResourceNotFound(ServiceError):
    status_code = 404


class ResourceConflict(ServiceError):
    status_code = 409


class InvalidRequest(ServiceError):
    status_code = 422


def _audit(
    *,
    actor_id: UUID,
    action: str,
    resource_type: str,
    resource_id: UUID | str,
    project_id: UUID | None,
    details: dict[str, object] | None = None,
) -> AuditLogRecord:
    return AuditLogRecord(
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id),
        project_id=project_id,
        details=details or {},
    )


async def _commit_unique(session: AsyncSession, message: str) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ResourceConflict(message) from exc


async def _project(session: AsyncSession, project_id: UUID) -> ProjectRecord:
    record = await session.get(ProjectRecord, project_id)
    if record is None:
        raise ResourceNotFound("project not found")
    return record


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _locked_project(session: AsyncSession, project_id: UUID) -> ProjectRecord:
    record = await session.scalar(
        select(ProjectRecord).where(ProjectRecord.id == project_id).with_for_update()
    )
    if record is None:
        raise ResourceNotFound("project not found")
    return record


async def _execution_usage(
    session: AsyncSession,
    project_id: UUID,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, int], datetime]:
    moment = now or utc_now()
    daily_window_started_at = datetime.combine(moment.date(), time.min, tzinfo=UTC)
    status_rows = await session.execute(
        select(TestRunRecord.status, func.count())
        .where(
            TestRunRecord.project_id == project_id,
            TestRunRecord.status.in_(status.value for status in IN_FLIGHT_RUN_STATUSES),
        )
        .group_by(TestRunRecord.status)
    )
    counts = {status: int(count) for status, count in status_rows}
    queued_runs = counts.get(RunStatus.QUEUED.value, 0)
    preparing_runs = counts.get(RunStatus.PREPARING.value, 0)
    running_runs = counts.get(RunStatus.RUNNING.value, 0)
    runs_created_today = await session.scalar(
        select(func.count())
        .select_from(TestRunRecord)
        .where(
            TestRunRecord.project_id == project_id,
            TestRunRecord.created_at >= daily_window_started_at,
        )
    )
    return (
        {
            "queued_runs": queued_runs,
            "preparing_runs": preparing_runs,
            "running_runs": running_runs,
            "in_flight_runs": queued_runs + preparing_runs + running_runs,
            "runs_created_today": int(runs_created_today or 0),
        },
        daily_window_started_at,
    )


async def _execution_policy_payload(
    session: AsyncSession,
    project: ProjectRecord,
) -> dict[str, object]:
    usage, daily_window_started_at = await _execution_usage(session, project.id)
    remaining_in_flight = max(0, project.max_in_flight_runs - usage["in_flight_runs"])
    remaining_daily = max(0, project.max_daily_runs - usage["runs_created_today"])
    blocked = remaining_in_flight == 0 or remaining_daily == 0
    near_limit = (
        usage["in_flight_runs"] * 100 >= project.max_in_flight_runs * 80
        or usage["runs_created_today"] * 100 >= project.max_daily_runs * 80
    )
    quota_status = "BLOCKED" if blocked else "NEAR_LIMIT" if near_limit else "AVAILABLE"
    return {
        "project_id": project.id,
        "max_in_flight_runs": project.max_in_flight_runs,
        "max_daily_runs": project.max_daily_runs,
        "run_timeout_seconds": project.run_timeout_seconds,
        **usage,
        "remaining_in_flight_runs": remaining_in_flight,
        "remaining_daily_runs": remaining_daily,
        "quota_status": quota_status,
        "daily_window_started_at": daily_window_started_at,
        "generated_at": utc_now(),
        "updated_at": project.updated_at,
    }


async def _enforce_execution_policy(
    session: AsyncSession,
    project: ProjectRecord,
) -> None:
    usage, _ = await _execution_usage(session, project.id)
    if usage["in_flight_runs"] >= project.max_in_flight_runs:
        raise ResourceConflict(
            "project in-flight Run quota reached "
            f"({usage['in_flight_runs']}/{project.max_in_flight_runs})"
        )
    if usage["runs_created_today"] >= project.max_daily_runs:
        raise ResourceConflict(
            "project daily Run quota reached "
            f"({usage['runs_created_today']}/{project.max_daily_runs})"
        )


async def _append_run_event(
    session: AsyncSession,
    run: TestRunRecord,
    *,
    dedupe_key: str,
    source: str,
    event_type: str,
    occurred_at: datetime,
    case_code: str | None = None,
    status: str | None = None,
    payload: dict[str, object] | None = None,
) -> tuple[RunEventRecord, bool]:
    existing = await session.scalar(
        select(RunEventRecord).where(
            RunEventRecord.run_id == run.id,
            RunEventRecord.dedupe_key == dedupe_key,
        )
    )
    if existing is not None:
        return existing, False
    current_sequence = await session.scalar(
        select(func.max(RunEventRecord.sequence)).where(RunEventRecord.run_id == run.id)
    )
    event = RunEventRecord(
        run_id=run.id,
        sequence=int(current_sequence or 0) + 1,
        dedupe_key=dedupe_key,
        source=source,
        event_type=event_type,
        case_code=case_code,
        status=status,
        payload=payload or {},
        occurred_at=occurred_at,
    )
    session.add(event)
    return event, True


async def create_project(
    session: AsyncSession,
    payload: ProjectCreate,
    actor_id: UUID,
) -> ProjectRecord:
    record = ProjectRecord(
        id=uuid4(),
        key=payload.key,
        name=payload.name,
        description=payload.description,
    )
    session.add(record)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ResourceConflict("project key already exists") from exc
    session.add(
        ProjectMemberRecord(
            id=uuid4(),
            project_id=record.id,
            user_id=actor_id,
            role="PROJECT_ADMIN",
            created_by=actor_id,
        )
    )
    session.add(
        _audit(
            actor_id=actor_id,
            action="project.created",
            resource_type="project",
            resource_id=record.id,
            project_id=record.id,
            details={"key": record.key},
        )
    )
    await session.commit()
    await session.refresh(record)
    return record


async def list_projects(
    session: AsyncSession,
    *,
    user_id: UUID,
    is_system_admin: bool,
) -> tuple[ProjectRecord, ...]:
    query = select(ProjectRecord).order_by(ProjectRecord.created_at)
    if not is_system_admin:
        query = query.join(
            ProjectMemberRecord,
            ProjectMemberRecord.project_id == ProjectRecord.id,
        ).where(ProjectMemberRecord.user_id == user_id)
    result = await session.scalars(query)
    return tuple(result)


async def update_project(
    session: AsyncSession,
    project_id: UUID,
    payload: ProjectUpdate,
    actor_id: UUID,
) -> ProjectRecord:
    record = await _project(session, project_id)
    before = {
        "name": record.name,
        "description": record.description,
        "status": record.status,
    }
    if payload.name is not None:
        record.name = payload.name
    if "description" in payload.model_fields_set:
        record.description = payload.description
    if payload.status is not None:
        record.status = payload.status
    session.add(
        _audit(
            actor_id=actor_id,
            action="project.updated",
            resource_type="project",
            resource_id=record.id,
            project_id=record.id,
            details={
                "before": before,
                "after": {
                    "name": record.name,
                    "description": record.description,
                    "status": record.status,
                },
            },
        )
    )
    await session.commit()
    await session.refresh(record)
    return record


async def get_execution_policy(
    session: AsyncSession,
    project_id: UUID,
) -> dict[str, object]:
    project = await _project(session, project_id)
    return await _execution_policy_payload(session, project)


async def update_execution_policy(
    session: AsyncSession,
    project_id: UUID,
    payload: ExecutionPolicyUpdate,
    actor_id: UUID,
) -> dict[str, object]:
    project = await _locked_project(session, project_id)
    before = {
        "max_in_flight_runs": project.max_in_flight_runs,
        "max_daily_runs": project.max_daily_runs,
        "run_timeout_seconds": project.run_timeout_seconds,
    }
    if payload.max_in_flight_runs is not None:
        project.max_in_flight_runs = payload.max_in_flight_runs
    if payload.max_daily_runs is not None:
        project.max_daily_runs = payload.max_daily_runs
    if payload.run_timeout_seconds is not None:
        project.run_timeout_seconds = payload.run_timeout_seconds
    after = {
        "max_in_flight_runs": project.max_in_flight_runs,
        "max_daily_runs": project.max_daily_runs,
        "run_timeout_seconds": project.run_timeout_seconds,
    }
    session.add(
        _audit(
            actor_id=actor_id,
            action="project.execution_policy_updated",
            resource_type="project_execution_policy",
            resource_id=project.id,
            project_id=project.id,
            details={"before": before, "after": after},
        )
    )
    await session.commit()
    await session.refresh(project)
    return await _execution_policy_payload(session, project)


async def _runner_pool(session: AsyncSession, pool_id: UUID) -> RunnerPoolRecord:
    record = await session.get(RunnerPoolRecord, pool_id)
    if record is None:
        raise ResourceNotFound("Runner Pool not found")
    return record


async def _runner_pool_for_target(
    session: AsyncSession,
    pool_id: UUID | None,
    target_type: str,
) -> RunnerPoolRecord | None:
    if pool_id is None:
        return None
    pool = await _runner_pool(session, pool_id)
    if pool.status != "ACTIVE":
        raise ResourceConflict("only an ACTIVE Runner Pool can be bound")
    if target_type not in pool.target_types:
        raise InvalidRequest(f"Runner Pool does not support target type {target_type}")
    return pool


async def _runner_pool_payload(
    session: AsyncSession,
    pool: RunnerPoolRecord,
    *,
    heartbeat_ttl_seconds: int,
    now: datetime | None = None,
) -> dict[str, object]:
    moment = now or utc_now()
    healthy_after = moment - timedelta(seconds=heartbeat_ttl_seconds)
    healthy_workers = tuple(
        await session.scalars(
            select(RunnerWorkerRecord).where(
                RunnerWorkerRecord.pool_id == pool.id,
                RunnerWorkerRecord.status == "ACTIVE",
                RunnerWorkerRecord.last_heartbeat_at >= healthy_after,
            )
        )
    )
    active_leases = int(
        await session.scalar(
            select(func.count())
            .select_from(RunnerSlotLeaseRecord)
            .where(
                RunnerSlotLeaseRecord.pool_id == pool.id,
                RunnerSlotLeaseRecord.status == "ACTIVE",
            )
        )
        or 0
    )
    total_worker_slots = sum(worker.max_slots for worker in healthy_workers)
    effective_capacity = min(pool.max_concurrency, total_worker_slots)
    return {
        "id": pool.id,
        "key": pool.key,
        "name": pool.name,
        "description": pool.description,
        "target_types": tuple(pool.target_types),
        "queue_name": pool.queue_name,
        "max_concurrency": pool.max_concurrency,
        "status": pool.status,
        "healthy_workers": len(healthy_workers),
        "total_worker_slots": total_worker_slots,
        "active_leases": active_leases,
        "available_slots": max(0, effective_capacity - active_leases),
        "created_at": pool.created_at,
        "updated_at": pool.updated_at,
    }


async def create_runner_pool(
    session: AsyncSession,
    payload: RunnerPoolCreate,
    actor_id: UUID,
    *,
    heartbeat_ttl_seconds: int,
) -> dict[str, object]:
    record = RunnerPoolRecord(
        id=uuid4(),
        key=payload.key,
        name=payload.name,
        description=payload.description,
        target_types=[item.value for item in payload.target_types],
        queue_name=f"testops.pool.{payload.key}",
        max_concurrency=payload.max_concurrency,
    )
    session.add(record)
    session.add(
        _audit(
            actor_id=actor_id,
            action="runner.pool_created",
            resource_type="runner_pool",
            resource_id=record.id,
            project_id=None,
            details={
                "key": record.key,
                "target_types": record.target_types,
                "max_concurrency": record.max_concurrency,
            },
        )
    )
    await _commit_unique(session, "Runner Pool key already exists")
    await session.refresh(record)
    return await _runner_pool_payload(
        session,
        record,
        heartbeat_ttl_seconds=heartbeat_ttl_seconds,
    )


async def list_runner_pools(
    session: AsyncSession,
    *,
    heartbeat_ttl_seconds: int,
    active_only: bool = False,
) -> tuple[dict[str, object], ...]:
    statement = select(RunnerPoolRecord)
    if active_only:
        statement = statement.where(RunnerPoolRecord.status == "ACTIVE")
    pools = tuple(await session.scalars(statement.order_by(RunnerPoolRecord.created_at)))
    return tuple(
        [
            await _runner_pool_payload(
                session,
                pool,
                heartbeat_ttl_seconds=heartbeat_ttl_seconds,
            )
            for pool in pools
        ]
    )


async def update_runner_pool(
    session: AsyncSession,
    pool_id: UUID,
    payload: RunnerPoolUpdate,
    actor_id: UUID,
    *,
    heartbeat_ttl_seconds: int,
) -> dict[str, object]:
    pool = await session.scalar(
        select(RunnerPoolRecord).where(RunnerPoolRecord.id == pool_id).with_for_update()
    )
    if pool is None:
        raise ResourceNotFound("Runner Pool not found")
    before = {
        "name": pool.name,
        "description": pool.description,
        "target_types": list(pool.target_types),
        "max_concurrency": pool.max_concurrency,
        "status": pool.status,
    }
    if payload.name is not None:
        pool.name = payload.name
    if "description" in payload.model_fields_set:
        pool.description = payload.description
    if payload.target_types is not None:
        next_target_types = [item.value for item in payload.target_types]
        bound_target_types = set(
            await session.scalars(
                select(TestTargetRecord.target_type).where(
                    TestTargetRecord.runner_pool_id == pool.id
                )
            )
        )
        bound_target_types.update(
            await session.scalars(
                select(TestTargetRecord.target_type)
                .join(EnvironmentRecord, EnvironmentRecord.target_id == TestTargetRecord.id)
                .where(EnvironmentRecord.runner_pool_id == pool.id)
            )
        )
        if bound_target_types - set(next_target_types):
            raise ResourceConflict("Runner Pool target types are still used by project bindings")
        pool.target_types = next_target_types
    if payload.max_concurrency is not None:
        pool.max_concurrency = payload.max_concurrency
    if payload.status is not None:
        pool.status = payload.status
    after = {
        "name": pool.name,
        "description": pool.description,
        "target_types": list(pool.target_types),
        "max_concurrency": pool.max_concurrency,
        "status": pool.status,
    }
    session.add(
        _audit(
            actor_id=actor_id,
            action="runner.pool_updated",
            resource_type="runner_pool",
            resource_id=pool.id,
            project_id=None,
            details={"before": before, "after": after},
        )
    )
    await session.commit()
    await session.refresh(pool)
    return await _runner_pool_payload(
        session,
        pool,
        heartbeat_ttl_seconds=heartbeat_ttl_seconds,
    )


async def heartbeat_runner_worker(
    session: AsyncSession,
    worker_key: str,
    payload: RunnerWorkerHeartbeat,
) -> tuple[RunnerWorkerRecord, RunnerPoolRecord]:
    pool = await session.scalar(
        select(RunnerPoolRecord).where(RunnerPoolRecord.key == payload.pool_key)
    )
    if pool is None:
        raise ResourceNotFound("Runner Pool not found for worker heartbeat")
    advertised_types = {item.value for item in payload.capabilities.target_types}
    if not advertised_types.intersection(pool.target_types):
        raise InvalidRequest("Runner capabilities do not match the Runner Pool target types")
    record = await session.scalar(
        select(RunnerWorkerRecord)
        .where(RunnerWorkerRecord.worker_key == worker_key)
        .with_for_update()
    )
    created = record is None
    if record is None:
        record = RunnerWorkerRecord(
            id=uuid4(),
            pool_id=pool.id,
            worker_key=worker_key,
            display_name=payload.display_name,
            runner_version=payload.runner_version,
            capabilities=payload.capabilities.model_dump(mode="json"),
            max_slots=payload.max_slots,
            status="ACTIVE",
            last_heartbeat_at=utc_now(),
        )
        session.add(record)
    else:
        if record.pool_id != pool.id:
            raise ResourceConflict("Runner worker key is already registered in another pool")
        record.display_name = payload.display_name
        record.runner_version = payload.runner_version
        record.capabilities = payload.capabilities.model_dump(mode="json")
        record.max_slots = payload.max_slots
        record.last_heartbeat_at = utc_now()
    if created:
        session.add(
            _audit(
                actor_id=RUNNER_ACTOR_ID,
                action="runner.worker_registered",
                resource_type="runner_worker",
                resource_id=record.id,
                project_id=None,
                details={"worker_key": worker_key, "pool_key": pool.key},
            )
        )
    await session.commit()
    await session.refresh(record)
    return record, pool


async def list_runner_workers(
    session: AsyncSession,
    *,
    pool_id: UUID | None = None,
) -> tuple[tuple[RunnerWorkerRecord, RunnerPoolRecord], ...]:
    statement = select(RunnerWorkerRecord, RunnerPoolRecord).join(
        RunnerPoolRecord,
        RunnerPoolRecord.id == RunnerWorkerRecord.pool_id,
    )
    if pool_id is not None:
        statement = statement.where(RunnerWorkerRecord.pool_id == pool_id)
    rows = await session.execute(statement.order_by(RunnerWorkerRecord.worker_key))
    return tuple(rows)


async def update_runner_worker(
    session: AsyncSession,
    worker_id: UUID,
    payload: RunnerWorkerUpdate,
    actor_id: UUID,
) -> tuple[RunnerWorkerRecord, RunnerPoolRecord]:
    record = await session.scalar(
        select(RunnerWorkerRecord).where(RunnerWorkerRecord.id == worker_id).with_for_update()
    )
    if record is None:
        raise ResourceNotFound("Runner worker not found")
    previous_status = record.status
    record.status = payload.status
    session.add(
        _audit(
            actor_id=actor_id,
            action="runner.worker_status_updated",
            resource_type="runner_worker",
            resource_id=record.id,
            project_id=None,
            details={"previous_status": previous_status, "status": record.status},
        )
    )
    await session.commit()
    await session.refresh(record)
    pool = await _runner_pool(session, record.pool_id)
    return record, pool


async def create_target(
    session: AsyncSession,
    project_id: UUID,
    payload: TargetCreate,
    actor_id: UUID,
) -> TestTargetRecord:
    await _project(session, project_id)
    browser = payload.browser
    if payload.target_type == TargetType.WEB:
        browser = browser or "chromium"
        if browser not in {"chromium", "firefox", "webkit"}:
            raise InvalidRequest("WEB target browser must be chromium, firefox or webkit")
    elif browser:
        raise InvalidRequest("browser is only valid for WEB targets")
    await _runner_pool_for_target(session, payload.runner_pool_id, payload.target_type.value)
    record = TestTargetRecord(
        id=uuid4(),
        project_id=project_id,
        key=payload.key,
        name=payload.name,
        target_type=payload.target_type.value,
        browser=browser,
        runner_pool_id=payload.runner_pool_id,
    )
    session.add(record)
    session.add(
        _audit(
            actor_id=actor_id,
            action="target.created",
            resource_type="test_target",
            resource_id=record.id,
            project_id=project_id,
            details={
                "key": record.key,
                "target_type": record.target_type,
                "runner_pool_id": str(record.runner_pool_id) if record.runner_pool_id else None,
            },
        )
    )
    await _commit_unique(session, "target key already exists in this project")
    await session.refresh(record)
    return record


async def list_targets(
    session: AsyncSession,
    project_id: UUID,
) -> tuple[TestTargetRecord, ...]:
    await _project(session, project_id)
    result = await session.scalars(
        select(TestTargetRecord)
        .where(TestTargetRecord.project_id == project_id)
        .order_by(TestTargetRecord.created_at)
    )
    return tuple(result)


async def update_target(
    session: AsyncSession,
    project_id: UUID,
    target_id: UUID,
    payload: TargetUpdate,
    actor_id: UUID,
) -> TestTargetRecord:
    await _project(session, project_id)
    record = await session.get(TestTargetRecord, target_id)
    if record is None or record.project_id != project_id:
        raise ResourceNotFound("target not found in project")
    before = {
        "name": record.name,
        "browser": record.browser,
        "status": record.status,
        "runner_pool_id": str(record.runner_pool_id) if record.runner_pool_id else None,
    }
    if payload.name is not None:
        record.name = payload.name
    if "browser" in payload.model_fields_set:
        if record.target_type != TargetType.WEB.value and payload.browser is not None:
            raise InvalidRequest("browser is only valid for WEB targets")
        record.browser = payload.browser or (
            "chromium" if record.target_type == TargetType.WEB.value else None
        )
    if payload.status is not None:
        record.status = payload.status
    if "runner_pool_id" in payload.model_fields_set:
        await _runner_pool_for_target(session, payload.runner_pool_id, record.target_type)
        record.runner_pool_id = payload.runner_pool_id
    session.add(
        _audit(
            actor_id=actor_id,
            action="target.updated",
            resource_type="test_target",
            resource_id=record.id,
            project_id=project_id,
            details={
                "before": before,
                "after": {
                    "name": record.name,
                    "browser": record.browser,
                    "status": record.status,
                    "runner_pool_id": (
                        str(record.runner_pool_id) if record.runner_pool_id else None
                    ),
                },
            },
        )
    )
    await session.commit()
    await session.refresh(record)
    return record


def environment_document(payload: EnvironmentCreate) -> dict[str, object]:
    return {
        "web_config": payload.web_config.model_dump(mode="json") if payload.web_config else None,
        "variables": [item.model_dump(mode="json") for item in payload.variables],
        "secret_bindings": [item.model_dump(mode="json") for item in payload.secret_bindings],
    }


def _validate_environment_payload(target: TestTargetRecord, payload: EnvironmentCreate) -> None:
    if target.target_type == TargetType.WEB.value and payload.web_config is None:
        raise InvalidRequest("WEB target environment requires web_config")
    if target.target_type != TargetType.WEB.value and payload.web_config is not None:
        raise InvalidRequest("web_config is only valid for WEB target environments")
    names = [item.name for item in (*payload.variables, *payload.secret_bindings)]
    if len(names) != len(set(names)):
        raise InvalidRequest("environment contains duplicate variable binding")


async def create_environment(
    session: AsyncSession,
    project_id: UUID,
    target_id: UUID,
    payload: EnvironmentCreate,
    actor_id: UUID,
) -> EnvironmentRecord:
    await _project(session, project_id)
    target = await session.get(TestTargetRecord, target_id)
    if target is None or target.project_id != project_id:
        raise ResourceNotFound("target not found in project")
    _validate_environment_payload(target, payload)
    await _runner_pool_for_target(session, payload.runner_pool_id, target.target_type)
    document = environment_document(payload)
    record = EnvironmentRecord(
        id=uuid4(),
        project_id=project_id,
        target_id=target_id,
        runner_pool_id=payload.runner_pool_id,
        key=payload.key,
        name=payload.name,
        config_document=document,
        config_hash=canonical_sha256(document),
    )
    session.add(record)
    session.add(
        _audit(
            actor_id=actor_id,
            action="environment.created",
            resource_type="environment",
            resource_id=record.id,
            project_id=project_id,
            details={
                "key": record.key,
                "config_hash": record.config_hash,
                "runner_pool_id": str(record.runner_pool_id) if record.runner_pool_id else None,
            },
        )
    )
    await _commit_unique(session, "environment key already exists for this target")
    await session.refresh(record)
    return record


async def list_environments(
    session: AsyncSession,
    project_id: UUID,
    target_id: UUID,
) -> tuple[EnvironmentRecord, ...]:
    await _project(session, project_id)
    result = await session.scalars(
        select(EnvironmentRecord)
        .where(
            EnvironmentRecord.project_id == project_id,
            EnvironmentRecord.target_id == target_id,
        )
        .order_by(EnvironmentRecord.created_at)
    )
    return tuple(result)


async def update_environment(
    session: AsyncSession,
    project_id: UUID,
    target_id: UUID,
    environment_id: UUID,
    payload: EnvironmentUpdate,
    actor_id: UUID,
) -> EnvironmentRecord:
    await _project(session, project_id)
    target = await session.get(TestTargetRecord, target_id)
    if target is None or target.project_id != project_id:
        raise ResourceNotFound("target not found in project")
    record = await session.get(EnvironmentRecord, environment_id)
    if record is None or record.project_id != project_id or record.target_id != target_id:
        raise ResourceNotFound("environment not found for target")
    current = record.config_document
    web_config = (
        payload.web_config
        if "web_config" in payload.model_fields_set
        else current.get("web_config")
    )
    variables = payload.variables if payload.variables is not None else current.get("variables", [])
    secret_bindings = (
        payload.secret_bindings
        if payload.secret_bindings is not None
        else current.get("secret_bindings", [])
    )
    merged = EnvironmentCreate.model_validate(
        {
            "key": record.key,
            "name": payload.name or record.name,
            "web_config": web_config,
            "variables": variables,
            "secret_bindings": secret_bindings,
        }
    )
    _validate_environment_payload(target, merged)
    before_hash = record.config_hash
    record.name = merged.name
    record.config_document = environment_document(merged)
    record.config_hash = canonical_sha256(record.config_document)
    if payload.status is not None:
        record.status = payload.status
    if "runner_pool_id" in payload.model_fields_set:
        await _runner_pool_for_target(session, payload.runner_pool_id, target.target_type)
        record.runner_pool_id = payload.runner_pool_id
    session.add(
        _audit(
            actor_id=actor_id,
            action="environment.updated",
            resource_type="environment",
            resource_id=record.id,
            project_id=project_id,
            details={
                "key": record.key,
                "previous_config_hash": before_hash,
                "config_hash": record.config_hash,
                "status": record.status,
                "runner_pool_id": str(record.runner_pool_id) if record.runner_pool_id else None,
            },
        )
    )
    await session.commit()
    await session.refresh(record)
    return record


async def publish_baseline(
    session: AsyncSession,
    project_id: UUID,
    payload: BaselinePublishRequest,
    actor_id: UUID,
) -> tuple[CaseBaselineRecord, bool]:
    project = await _project(session, project_id)
    baseline = payload.baseline
    if baseline.project_key != project.key:
        raise InvalidRequest("baseline project_key does not match project")
    computed_digest = canonical_sha256(baseline)
    if computed_digest != payload.digest:
        raise InvalidRequest("baseline digest does not match canonical document")

    existing = await session.scalar(
        select(CaseBaselineRecord).where(
            CaseBaselineRecord.project_id == project_id,
            CaseBaselineRecord.version == baseline.version,
        )
    )
    document = baseline.model_dump(mode="json", exclude_none=True)
    if existing is not None:
        if (
            existing.baseline_id == baseline.baseline_id
            and existing.digest == payload.digest
            and existing.document == document
        ):
            return existing, False
        raise ResourceConflict("baseline version is immutable and already published")

    record = CaseBaselineRecord(
        baseline_id=baseline.baseline_id,
        project_id=project_id,
        version=baseline.version,
        digest=payload.digest,
        case_count=len(baseline.cases),
        enabled_case_count=sum(case.enabled for case in baseline.cases),
        source_kind=baseline.source.kind,
        document=document,
    )
    session.add(record)
    session.add(
        _audit(
            actor_id=actor_id,
            action="baseline.published",
            resource_type="case_baseline",
            resource_id=baseline.baseline_id,
            project_id=project_id,
            details={
                "version": baseline.version,
                "digest": payload.digest,
                "case_count": len(baseline.cases),
            },
        )
    )
    await _commit_unique(session, "baseline ID, version or digest already exists")
    await session.refresh(record)
    return record, True


async def list_baselines(
    session: AsyncSession,
    project_id: UUID,
) -> tuple[CaseBaselineRecord, ...]:
    await _project(session, project_id)
    result = await session.scalars(
        select(CaseBaselineRecord)
        .where(
            CaseBaselineRecord.project_id == project_id,
            CaseBaselineRecord.status == "RELEASED",
        )
        .order_by(CaseBaselineRecord.created_at)
    )
    return tuple(result)


async def get_baseline(
    session: AsyncSession,
    project_id: UUID,
    baseline_id: UUID,
) -> CaseBaselineRecord:
    record = await session.get(CaseBaselineRecord, baseline_id)
    if record is None or record.project_id != project_id:
        raise ResourceNotFound("baseline not found in project")
    return record


async def create_automation_package(
    session: AsyncSession,
    project_id: UUID,
    target_id: UUID,
    payload: AutomationPackageCreate | AutomationPackageDraftCreate,
    actor_id: UUID,
    *,
    status: str = "ACTIVE",
) -> AutomationPackageRecord:
    await _project(session, project_id)
    target = await session.get(TestTargetRecord, target_id)
    if target is None or target.project_id != project_id:
        raise ResourceNotFound("target not found in project")
    if target.target_type != TargetType.WEB.value or payload.runner_type != "WEB_PLAYWRIGHT":
        raise InvalidRequest("WEB_PLAYWRIGHT packages require a WEB target")
    supersedes_id = (
        payload.supersedes_id if isinstance(payload, AutomationPackageDraftCreate) else None
    )
    if supersedes_id is not None:
        superseded = await session.get(AutomationPackageRecord, supersedes_id)
        if (
            superseded is None
            or superseded.project_id != project_id
            or superseded.target_id != target_id
            or superseded.name != payload.name
        ):
            raise InvalidRequest("supersedes_id must reference the same target and package name")
        if superseded.status == "REVOKED":
            raise ResourceConflict("a revoked automation package cannot be superseded")
    record = AutomationPackageRecord(
        id=uuid4(),
        project_id=project_id,
        target_id=target_id,
        name=payload.name,
        version=payload.version,
        digest=payload.digest,
        runner_type=payload.runner_type,
        image_repository=payload.image_repository,
        status=status,
        supersedes_id=supersedes_id,
    )
    session.add(record)
    session.add(
        _audit(
            actor_id=actor_id,
            action=(
                "automation_package.draft_created"
                if status == "DRAFT"
                else "automation_package.created"
            ),
            resource_type="automation_package",
            resource_id=record.id,
            project_id=project_id,
            details={
                "name": record.name,
                "version": record.version,
                "digest": record.digest,
                "runner_type": record.runner_type,
                "image_repository": record.image_repository,
                "status": record.status,
                "supersedes_id": str(supersedes_id) if supersedes_id else None,
            },
        )
    )
    await _commit_unique(session, "automation package version already exists for target")
    await session.refresh(record)
    return record


async def list_automation_packages(
    session: AsyncSession,
    project_id: UUID,
    target_id: UUID,
) -> tuple[AutomationPackageRecord, ...]:
    await _project(session, project_id)
    result = await session.scalars(
        select(AutomationPackageRecord)
        .where(
            AutomationPackageRecord.project_id == project_id,
            AutomationPackageRecord.target_id == target_id,
        )
        .order_by(AutomationPackageRecord.created_at)
    )
    return tuple(result)


async def get_automation_package(
    session: AsyncSession,
    project_id: UUID,
    target_id: UUID,
    package_id: UUID,
    *,
    lock: bool = False,
) -> AutomationPackageRecord:
    statement = select(AutomationPackageRecord).where(
        AutomationPackageRecord.id == package_id,
        AutomationPackageRecord.project_id == project_id,
        AutomationPackageRecord.target_id == target_id,
    )
    if lock:
        statement = statement.with_for_update()
    record = await session.scalar(statement)
    if record is None:
        raise ResourceNotFound("automation package not found for target")
    return record


async def activate_automation_package(
    session: AsyncSession,
    project_id: UUID,
    target_id: UUID,
    package_id: UUID,
    payload: AutomationPackageActivateRequest,
    actor_id: UUID,
) -> AutomationPackageRecord:
    package = await get_automation_package(
        session,
        project_id,
        target_id,
        package_id,
        lock=True,
    )
    if package.status == "ACTIVE" and package.validated_run_id == payload.validation_run_id:
        return package
    if package.status != "DRAFT":
        raise ResourceConflict("only a DRAFT automation package can be activated")
    validation_run = await session.get(TestRunRecord, payload.validation_run_id)
    if (
        validation_run is None
        or validation_run.project_id != project_id
        or validation_run.target_id != target_id
        or validation_run.automation_package_id != package.id
    ):
        raise InvalidRequest("validation_run_id does not validate this automation package")
    if validation_run.status != RunStatus.PASSED.value or validation_run.result_digest is None:
        raise ResourceConflict("automation package validation Run must finish PASSED")
    baseline_record = await session.get(CaseBaselineRecord, validation_run.baseline_id)
    if baseline_record is None or baseline_record.status != "RELEASED":
        raise ResourceConflict("automation package validation requires a RELEASED baseline")
    baseline = CaseBaseline.model_validate(baseline_record.document)
    enabled_case_count = sum(case.enabled for case in baseline.cases)
    if validation_run.case_count != enabled_case_count:
        raise ResourceConflict(
            "automation package activation requires a full enabled-case regression"
        )

    package.status = "ACTIVE"
    package.validated_run_id = validation_run.id
    package.activated_by = actor_id
    package.activated_at = utc_now()
    package.status_reason = None
    session.add(
        _audit(
            actor_id=actor_id,
            action="automation_package.activated",
            resource_type="automation_package",
            resource_id=package.id,
            project_id=project_id,
            details={
                "validation_run_id": str(validation_run.id),
                "digest": package.digest,
                "version": package.version,
            },
        )
    )
    await session.commit()
    await session.refresh(package)
    return package


async def deprecate_automation_package(
    session: AsyncSession,
    project_id: UUID,
    target_id: UUID,
    package_id: UUID,
    payload: AutomationPackageStatusChangeRequest,
    actor_id: UUID,
) -> AutomationPackageRecord:
    package = await get_automation_package(
        session,
        project_id,
        target_id,
        package_id,
        lock=True,
    )
    if package.status == "DEPRECATED" and package.status_reason == payload.reason:
        return package
    if package.status != "ACTIVE":
        raise ResourceConflict("only an ACTIVE automation package can be deprecated")
    active_schedule_id = await session.scalar(
        select(RegressionScheduleRecord.id)
        .where(
            RegressionScheduleRecord.automation_package_id == package.id,
            RegressionScheduleRecord.status == "ACTIVE",
        )
        .limit(1)
    )
    if active_schedule_id is not None:
        raise ResourceConflict("automation package is still used by an ACTIVE regression schedule")
    package.status = "DEPRECATED"
    package.status_reason = payload.reason
    session.add(
        _audit(
            actor_id=actor_id,
            action="automation_package.deprecated",
            resource_type="automation_package",
            resource_id=package.id,
            project_id=project_id,
            details={"reason": payload.reason, "version": package.version},
        )
    )
    await session.commit()
    await session.refresh(package)
    return package


async def revoke_automation_package(
    session: AsyncSession,
    project_id: UUID,
    target_id: UUID,
    package_id: UUID,
    payload: AutomationPackageStatusChangeRequest,
    actor_id: UUID,
) -> AutomationPackageRecord:
    package = await get_automation_package(
        session,
        project_id,
        target_id,
        package_id,
        lock=True,
    )
    if package.status == "REVOKED" and package.status_reason == payload.reason:
        return package
    if package.status == "REVOKED":
        raise ResourceConflict("automation package is already revoked")
    package.status = "REVOKED"
    package.status_reason = payload.reason
    session.add(
        _audit(
            actor_id=actor_id,
            action="automation_package.revoked",
            resource_type="automation_package",
            resource_id=package.id,
            project_id=project_id,
            details={"reason": payload.reason, "version": package.version},
        )
    )
    await session.commit()
    await session.refresh(package)
    return package


def _environment_contract(
    environment: EnvironmentRecord,
) -> tuple[WebRunConfig | None, tuple[RuntimeVariable, ...], tuple[SecretBinding, ...]]:
    document = environment.config_document
    raw_web_config = document.get("web_config")
    web_config = WebRunConfig.model_validate(raw_web_config) if raw_web_config else None
    variables = tuple(RuntimeVariable.model_validate(item) for item in document["variables"])
    secret_bindings = tuple(
        SecretBinding.model_validate(item) for item in document["secret_bindings"]
    )
    return web_config, variables, secret_bindings


def _select_cases(baseline: CaseBaseline, requested_codes: tuple[str, ...]) -> tuple:
    if len(requested_codes) != len(set(requested_codes)):
        raise InvalidRequest("case_codes contains duplicates")
    cases_by_code = {case.case_code: case for case in baseline.cases}
    if requested_codes:
        unknown = sorted(set(requested_codes) - cases_by_code.keys())
        if unknown:
            raise InvalidRequest(f"unknown case_codes: {', '.join(unknown)}")
        disabled = sorted(code for code in requested_codes if not cases_by_code[code].enabled)
        if disabled:
            raise InvalidRequest(f"disabled cases cannot be scheduled: {', '.join(disabled)}")
        requested = set(requested_codes)
        return tuple(case for case in baseline.cases if case.case_code in requested)
    return tuple(case for case in baseline.cases if case.enabled)


async def _existing_idempotent_run(
    session: AsyncSession,
    project_id: UUID,
    idempotency_key: str,
    request_hash: str,
) -> TestRunRecord | None:
    existing = await session.scalar(
        select(TestRunRecord).where(
            TestRunRecord.project_id == project_id,
            TestRunRecord.idempotency_key == idempotency_key,
        )
    )
    if existing is not None and existing.request_hash != request_hash:
        raise ResourceConflict("idempotency key was already used with a different request")
    return existing


async def _queue_snapshot_run(
    session: AsyncSession,
    snapshot: RunSnapshot,
    *,
    project_id: UUID,
    target_id: UUID,
    environment_id: UUID,
    baseline_id: UUID,
    automation_package_id: UUID,
    idempotency_key: str,
    request_hash: str,
    actor_id: UUID,
    runner_pool_id: UUID | None,
    timeout_seconds: int,
    source_run_id: UUID | None = None,
    retry_mode: str | None = None,
    regression_schedule_id: UUID | None = None,
    scheduled_for: datetime | None = None,
    commit: bool = True,
) -> tuple[TestRunRecord, bool]:
    snapshot_document = snapshot.model_dump(mode="json", exclude_none=True)
    snapshot_digest = canonical_sha256(snapshot)
    created_at = snapshot.created_at
    record = TestRunRecord(
        id=snapshot.run_id,
        project_id=project_id,
        target_id=target_id,
        environment_id=environment_id,
        baseline_id=baseline_id,
        automation_package_id=automation_package_id,
        runner_pool_id=runner_pool_id,
        regression_schedule_id=regression_schedule_id,
        scheduled_for=scheduled_for,
        source_run_id=source_run_id,
        retry_mode=retry_mode,
        status=RunStatus.QUEUED.value,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        snapshot_digest=snapshot_digest,
        snapshot=snapshot_document,
        case_count=len(snapshot.cases),
        timeout_seconds=timeout_seconds,
        created_by=actor_id,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(record)
    event_payload: dict[str, object] = {
        "case_count": len(snapshot.cases),
        "snapshot_digest": snapshot_digest,
        "runner_pool_id": str(runner_pool_id) if runner_pool_id else None,
        "timeout_seconds": timeout_seconds,
    }
    if source_run_id is not None:
        event_payload["source_run_id"] = str(source_run_id)
        event_payload["retry_mode"] = retry_mode or "FULL"
    if regression_schedule_id is not None:
        event_payload["regression_schedule_id"] = str(regression_schedule_id)
        event_payload["scheduled_for"] = scheduled_for.isoformat() if scheduled_for else None
    await _append_run_event(
        session,
        record,
        dedupe_key="system:run_created",
        source="SYSTEM",
        event_type="run_created",
        occurred_at=created_at,
        status=RunStatus.QUEUED.value,
        payload=event_payload,
    )
    session.add_all(
        RunCaseRecord(
            run_id=record.id,
            case_id=case.case_id,
            case_code=case.case_code,
            sequence=sequence,
            status=RunStatus.QUEUED.value,
        )
        for sequence, case in enumerate(snapshot.cases, start=1)
    )
    session.add(
        DispatchOutboxRecord(
            aggregate_type="test_run",
            aggregate_id=record.id,
            event_type="run.queued",
            dedupe_key=f"run.queued:{record.id}",
            payload={
                "run_snapshot": snapshot_document,
                "runner_pool_id": str(runner_pool_id) if runner_pool_id else None,
            },
        )
    )
    audit_action = (
        "run.scheduled"
        if regression_schedule_id is not None
        else "run.rerun_created"
        if source_run_id is not None
        else "run.created"
    )
    session.add(
        _audit(
            actor_id=actor_id,
            action=audit_action,
            resource_type="test_run",
            resource_id=record.id,
            project_id=project_id,
            details=event_payload,
        )
    )
    try:
        if commit:
            await session.commit()
        else:
            await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raced = await _existing_idempotent_run(
            session,
            project_id,
            idempotency_key,
            request_hash,
        )
        if raced is not None:
            return raced, False
        raise ResourceConflict("Run could not be created due to a uniqueness conflict") from exc
    await session.refresh(record)
    return record, True


async def create_run(
    session: AsyncSession,
    payload: RunCreate,
    *,
    idempotency_key: str,
    actor_id: UUID,
    allowed_baseline_statuses: frozenset[str] = frozenset({"RELEASED"}),
    allowed_package_statuses: frozenset[str] = frozenset({"ACTIVE"}),
    regression_schedule_id: UUID | None = None,
    scheduled_for: datetime | None = None,
    commit: bool = True,
) -> tuple[TestRunRecord, bool]:
    request_hash = canonical_sha256(payload.model_dump(mode="json"))
    existing = await _existing_idempotent_run(
        session,
        payload.project_id,
        idempotency_key,
        request_hash,
    )
    if existing is not None:
        return existing, False

    project = await _locked_project(session, payload.project_id)
    existing = await _existing_idempotent_run(
        session,
        payload.project_id,
        idempotency_key,
        request_hash,
    )
    if existing is not None:
        return existing, False

    target = await session.get(TestTargetRecord, payload.target_id)
    environment = await session.get(EnvironmentRecord, payload.environment_id)
    baseline_record = await session.get(CaseBaselineRecord, payload.baseline_id)
    package = await session.get(AutomationPackageRecord, payload.automation_package_id)
    if target is None or target.project_id != project.id:
        raise ResourceNotFound("target not found in project")
    if (
        environment is None
        or environment.project_id != project.id
        or environment.target_id != target.id
    ):
        raise ResourceNotFound("environment not found for target")
    if baseline_record is None or baseline_record.project_id != project.id:
        raise ResourceNotFound("baseline not found in project")
    if baseline_record.status not in allowed_baseline_statuses:
        raise InvalidRequest(
            f"baseline status {baseline_record.status} cannot be scheduled through this endpoint"
        )
    if package is None or package.project_id != project.id or package.target_id != target.id:
        raise ResourceNotFound("automation package not found for target")
    if package.status not in allowed_package_statuses:
        raise InvalidRequest(
            f"automation package status {package.status} cannot be scheduled through this endpoint"
        )
    if target.target_type != TargetType.WEB.value:
        raise InvalidRequest("M2 Runner dispatch currently supports WEB targets only")

    baseline = CaseBaseline.model_validate(baseline_record.document)
    cases = _select_cases(baseline, payload.case_codes)
    if not cases:
        raise InvalidRequest("Run must contain at least one enabled case")
    web_config, variables, secret_bindings = _environment_contract(environment)
    if web_config is None:
        raise InvalidRequest("WEB environment has no web_config")
    await _enforce_execution_policy(session, project)
    runner_pool_id = environment.runner_pool_id or target.runner_pool_id

    run_id = uuid4()
    created_at = utc_now()
    snapshot = RunSnapshot(
        run_id=run_id,
        project_id=project.id,
        target_id=target.id,
        target_type=TargetType(target.target_type),
        environment_id=environment.id,
        case_baseline=CaseBaselineRef(
            baseline_id=baseline.baseline_id,
            version=baseline.version,
            digest=baseline_record.digest,
            case_count=len(baseline.cases),
        ),
        automation_package=AutomationPackageRef(
            name=package.name,
            version=package.version,
            digest=package.digest,
            runner_type=package.runner_type,
            image_repository=package.image_repository,
        ),
        cases=cases,
        browser=target.browser,
        web_config=web_config,
        config_hash=environment.config_hash,
        variables=variables,
        secret_bindings=secret_bindings,
        created_by=actor_id,
        created_at=created_at,
    )
    return await _queue_snapshot_run(
        session,
        snapshot,
        project_id=project.id,
        target_id=target.id,
        environment_id=environment.id,
        baseline_id=baseline.baseline_id,
        automation_package_id=package.id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        actor_id=actor_id,
        runner_pool_id=runner_pool_id,
        timeout_seconds=project.run_timeout_seconds,
        regression_schedule_id=regression_schedule_id,
        scheduled_for=scheduled_for,
        commit=commit,
    )


async def create_automation_package_validation_run(
    session: AsyncSession,
    project_id: UUID,
    target_id: UUID,
    package_id: UUID,
    payload: AutomationPackageValidationRunCreate,
    *,
    idempotency_key: str,
    actor_id: UUID,
) -> tuple[TestRunRecord, bool]:
    package = await get_automation_package(
        session,
        project_id,
        target_id,
        package_id,
    )
    if package.status != "DRAFT":
        raise ResourceConflict("only a DRAFT automation package can start validation")
    run, created = await create_run(
        session,
        RunCreate(
            project_id=project_id,
            target_id=target_id,
            environment_id=payload.environment_id,
            baseline_id=payload.baseline_id,
            automation_package_id=package_id,
        ),
        idempotency_key=idempotency_key,
        actor_id=actor_id,
        allowed_package_statuses=frozenset({"DRAFT"}),
        commit=False,
    )
    if created:
        session.add(
            _audit(
                actor_id=actor_id,
                action="automation_package.validation_run_created",
                resource_type="automation_package",
                resource_id=package.id,
                project_id=project_id,
                details={
                    "validation_run_id": str(run.id),
                    "baseline_id": str(payload.baseline_id),
                    "environment_id": str(payload.environment_id),
                },
            )
        )
    await session.commit()
    await session.refresh(run)
    return run, created


async def create_rerun(
    session: AsyncSession,
    source_run_id: UUID,
    *,
    mode: str,
    idempotency_key: str,
    actor_id: UUID,
) -> tuple[TestRunRecord, bool]:
    source = await get_run(session, source_run_id)
    if RunStatus(source.status) not in TERMINAL_RUN_STATUSES:
        raise ResourceConflict("only a terminal Run can be rerun")
    request_hash = canonical_sha256({"source_run_id": str(source_run_id), "mode": mode})
    existing = await _existing_idempotent_run(
        session,
        source.project_id,
        idempotency_key,
        request_hash,
    )
    if existing is not None:
        return existing, False

    project = await _locked_project(session, source.project_id)
    existing = await _existing_idempotent_run(
        session,
        source.project_id,
        idempotency_key,
        request_hash,
    )
    if existing is not None:
        return existing, False

    source_snapshot = RunSnapshot.model_validate(source.snapshot)
    target = await session.get(TestTargetRecord, source.target_id)
    environment = await session.get(EnvironmentRecord, source.environment_id)
    package = await session.get(AutomationPackageRecord, source.automation_package_id)
    if target is None or environment is None or package is None:
        raise ResourceNotFound("source Run scheduling resources no longer exist")
    if package.status == "REVOKED":
        raise ResourceConflict("a Run using a revoked automation package cannot be rerun")
    runner_pool_id = environment.runner_pool_id or target.runner_pool_id
    cases = source_snapshot.cases
    if mode == "FAILED_ONLY":
        if source.result_document is None:
            raise ResourceConflict("failed-only rerun requires an immutable Run Result")
        result = RunResult.model_validate(source.result_document)
        failed_case_ids = {
            item.case_id
            for item in result.case_results
            if item.status in {CaseResultStatus.FAILED, CaseResultStatus.INFRA_ERROR}
        }
        cases = tuple(case for case in source_snapshot.cases if case.case_id in failed_case_ids)
        if not cases:
            raise ResourceConflict("source Run has no failed cases to rerun")
    await _enforce_execution_policy(session, project)

    created_at = utc_now()
    snapshot = source_snapshot.model_copy(
        update={
            "run_id": uuid4(),
            "cases": cases,
            "created_by": actor_id,
            "created_at": created_at,
        }
    )
    return await _queue_snapshot_run(
        session,
        snapshot,
        project_id=source.project_id,
        target_id=source.target_id,
        environment_id=source.environment_id,
        baseline_id=source.baseline_id,
        automation_package_id=source.automation_package_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        actor_id=actor_id,
        runner_pool_id=runner_pool_id,
        timeout_seconds=project.run_timeout_seconds,
        source_run_id=source.id,
        retry_mode=mode,
    )


async def get_run(session: AsyncSession, run_id: UUID) -> TestRunRecord:
    record = await session.get(TestRunRecord, run_id)
    if record is None:
        raise ResourceNotFound("Run not found")
    return record


async def list_run_cases(
    session: AsyncSession,
    run_id: UUID,
) -> tuple[RunCaseRecord, ...]:
    return tuple(
        await session.scalars(
            select(RunCaseRecord)
            .where(RunCaseRecord.run_id == run_id)
            .order_by(RunCaseRecord.sequence)
        )
    )


async def list_run_artifacts(
    session: AsyncSession,
    run_id: UUID,
) -> tuple[ArtifactRecord, ...]:
    return tuple(
        await session.scalars(
            select(ArtifactRecord)
            .where(ArtifactRecord.run_id == run_id)
            .order_by(ArtifactRecord.created_at, ArtifactRecord.artifact_id)
        )
    )


async def get_run_artifact(
    session: AsyncSession,
    run_id: UUID,
    artifact_id: UUID,
) -> ArtifactRecord:
    artifact = await session.scalar(
        select(ArtifactRecord).where(
            ArtifactRecord.run_id == run_id,
            ArtifactRecord.artifact_id == artifact_id,
        )
    )
    if artifact is None:
        raise ResourceNotFound("Run artifact not found")
    return artifact


async def audit_artifact_access(
    session: AsyncSession,
    artifact: ArtifactRecord,
    *,
    project_id: UUID,
    actor_id: UUID,
    expires_in_seconds: int,
) -> None:
    session.add(
        _audit(
            actor_id=actor_id,
            action="artifact.access_granted",
            resource_type="artifact",
            resource_id=artifact.artifact_id,
            project_id=project_id,
            details={
                "run_id": str(artifact.run_id),
                "kind": artifact.kind,
                "expires_in_seconds": expires_in_seconds,
            },
        )
    )
    await session.commit()


async def list_run_events(
    session: AsyncSession,
    run_id: UUID,
    *,
    after_sequence: int = 0,
    limit: int = 200,
) -> tuple[RunEventRecord, ...]:
    return tuple(
        await session.scalars(
            select(RunEventRecord)
            .where(
                RunEventRecord.run_id == run_id,
                RunEventRecord.sequence > after_sequence,
            )
            .order_by(RunEventRecord.sequence)
            .limit(limit)
        )
    )


async def record_run_event(
    session: AsyncSession,
    run_id: UUID,
    payload: InternalRunEventCreate,
) -> tuple[RunEventRecord, bool]:
    if payload.run_id != run_id:
        raise InvalidRequest("Run event run_id does not match URL")
    run = await _locked_run(session, run_id)
    document = payload.model_dump(mode="json", exclude_none=True)
    dedupe_key = canonical_sha256(document)
    existing = await session.scalar(
        select(RunEventRecord).where(
            RunEventRecord.run_id == run_id,
            RunEventRecord.dedupe_key == dedupe_key,
        )
    )
    if existing is not None:
        return existing, False

    run_case: RunCaseRecord | None = None
    if payload.case_code is not None:
        run_case = await session.scalar(
            select(RunCaseRecord).where(
                RunCaseRecord.run_id == run_id,
                RunCaseRecord.case_code == payload.case_code,
            )
        )
        if run_case is None:
            raise InvalidRequest("Run event case_code is not in the immutable Run Snapshot")

    status_value = payload.status.value if payload.status is not None else None
    event, created = await _append_run_event(
        session,
        run,
        dedupe_key=dedupe_key,
        source="RUNNER",
        event_type=payload.event,
        occurred_at=payload.at,
        case_code=payload.case_code,
        status=status_value,
        payload=document,
    )
    if created and RunStatus(run.status) not in TERMINAL_RUN_STATUSES and run_case is not None:
        if payload.event == "case_started":
            run_case.status = "RUNNING"
        elif payload.event == "case_finished" and status_value is not None:
            run_case.status = status_value
    await session.commit()
    await session.refresh(event)
    return event, created


async def _locked_run(session: AsyncSession, run_id: UUID) -> TestRunRecord:
    record = await session.scalar(
        select(TestRunRecord).where(TestRunRecord.id == run_id).with_for_update()
    )
    if record is None:
        raise ResourceNotFound("Run not found")
    return record


async def _release_runner_slot(session: AsyncSession, run_id: UUID) -> bool:
    lease = await session.scalar(
        select(RunnerSlotLeaseRecord)
        .where(
            RunnerSlotLeaseRecord.run_id == run_id,
            RunnerSlotLeaseRecord.status == "ACTIVE",
        )
        .with_for_update()
    )
    if lease is None:
        return False
    lease.status = "RELEASED"
    lease.released_at = utc_now()
    return True


async def _bind_runner_slot(
    session: AsyncSession,
    run: TestRunRecord,
    worker_key: str | None,
) -> bool:
    if worker_key is None or run.runner_pool_id is None:
        return False
    lease = await session.scalar(
        select(RunnerSlotLeaseRecord)
        .where(
            RunnerSlotLeaseRecord.run_id == run.id,
            RunnerSlotLeaseRecord.status == "ACTIVE",
        )
        .with_for_update()
    )
    if lease is None:
        raise ResourceConflict("pooled Run has no active Runner slot lease")
    worker = await session.scalar(
        select(RunnerWorkerRecord)
        .where(RunnerWorkerRecord.worker_key == worker_key)
        .with_for_update()
    )
    if worker is None or worker.pool_id != lease.pool_id:
        raise InvalidRequest("Runner worker does not belong to the Run pool")
    if worker.status != "ACTIVE":
        raise ResourceConflict("Runner worker is not ACTIVE")
    if lease.worker_id is not None and lease.worker_id != worker.id:
        raise ResourceConflict("Runner slot lease is already bound to another worker")
    lease.worker_id = worker.id
    worker.last_heartbeat_at = utc_now()
    return True


async def update_run_status(
    session: AsyncSession,
    run_id: UUID,
    target: RunStatus,
    *,
    worker_key: str | None = None,
) -> tuple[TestRunRecord, bool]:
    if target not in {RunStatus.PREPARING, RunStatus.RUNNING}:
        raise InvalidRequest("Runner status callback only accepts PREPARING or RUNNING")
    record = await _locked_run(session, run_id)
    current = RunStatus(record.status)
    lease_bound = await _bind_runner_slot(session, record, worker_key)
    if current == target:
        if lease_bound:
            await session.commit()
            await session.refresh(record)
        return record, False
    try:
        require_run_transition(current, target)
    except InvalidRunTransition as exc:
        raise ResourceConflict(str(exc)) from exc
    record.status = target.value
    changed_at = utc_now()
    if target in {RunStatus.PREPARING, RunStatus.RUNNING} and record.timeout_at is None:
        record.timeout_at = changed_at + timedelta(seconds=record.timeout_seconds)
    if target == RunStatus.RUNNING and record.started_at is None:
        record.started_at = changed_at
    await _append_run_event(
        session,
        record,
        dedupe_key=f"system:status:{target.value}",
        source="SYSTEM",
        event_type="status_changed",
        occurred_at=changed_at,
        status=target.value,
        payload={"previous_status": current.value, "status": target.value},
    )
    session.add(
        _audit(
            actor_id=RUNNER_ACTOR_ID,
            action="run.status_changed",
            resource_type="test_run",
            resource_id=run_id,
            project_id=record.project_id,
            details={"previous_status": current.value, "status": target.value},
        )
    )
    await session.commit()
    await session.refresh(record)
    return record, True


async def record_run_result(
    session: AsyncSession,
    run_id: UUID,
    result: RunResult,
) -> tuple[TestRunRecord, bool]:
    if result.run_id != run_id:
        raise InvalidRequest("Run Result run_id does not match URL")
    record = await _locked_run(session, run_id)
    result_digest = canonical_sha256(result)
    result_document = result.model_dump(mode="json", exclude_none=True)
    if record.result_digest is not None:
        if record.result_digest == result_digest and record.result_document == result_document:
            return record, False
        raise ResourceConflict("Run Result is immutable and was already recorded")

    current = RunStatus(record.status)
    try:
        require_run_transition(current, result.status)
    except InvalidRunTransition as exc:
        raise ResourceConflict(str(exc)) from exc
    stored_cases = tuple(
        await session.scalars(
            select(RunCaseRecord)
            .where(RunCaseRecord.run_id == run_id)
            .order_by(RunCaseRecord.sequence)
        )
    )
    stored_by_id = {case.case_id: case for case in stored_cases}
    result_ids = [case_result.case_id for case_result in result.case_results]
    if len(result_ids) != len(set(result_ids)):
        raise InvalidRequest("Run Result contains duplicate case_id")
    if set(result_ids) != set(stored_by_id):
        raise InvalidRequest("Run Result cases do not match the immutable Run Snapshot")
    result_statuses = {case_result.status for case_result in result.case_results}
    if result.status == RunStatus.PASSED and result_statuses - {
        CaseResultStatus.PASSED,
        CaseResultStatus.SKIPPED,
    }:
        raise InvalidRequest("PASSED Run Result cannot contain failed case results")
    if result.status == RunStatus.FAILED and CaseResultStatus.FAILED not in result_statuses:
        raise InvalidRequest("FAILED Run Result requires at least one FAILED case")
    if (
        result.status == RunStatus.INFRA_ERROR
        and CaseResultStatus.INFRA_ERROR not in result_statuses
    ):
        raise InvalidRequest("INFRA_ERROR Run Result requires at least one INFRA_ERROR case")
    for case_result in result.case_results:
        stored = stored_by_id[case_result.case_id]
        if stored.case_code != case_result.case_code:
            raise InvalidRequest("Run Result case_code does not match stored case_id")
        stored.status = case_result.status.value
        stored.duration_ms = case_result.duration_ms
        stored.failure_category = case_result.failure_category
        stored.error_message = case_result.error_message

    artifact_ids = [artifact.artifact_id for artifact in result.artifacts]
    if len(artifact_ids) != len(set(artifact_ids)):
        raise InvalidRequest("Run Result contains duplicate artifact_id")
    artifact_id_set = set(artifact_ids)
    unknown_artifact_refs = sorted(
        {
            str(artifact_id)
            for case_result in result.case_results
            for artifact_id in case_result.artifact_ids
            if artifact_id not in artifact_id_set
        }
    )
    if unknown_artifact_refs:
        raise InvalidRequest("Run Result case references unknown artifact_id")
    session.add_all(
        ArtifactRecord(
            artifact_id=artifact.artifact_id,
            run_id=run_id,
            kind=artifact.kind.value,
            name=artifact.name,
            uri=artifact.uri,
            digest=artifact.digest,
            size_bytes=artifact.size_bytes,
        )
        for artifact in result.artifacts
    )
    record.status = result.status.value
    record.started_at = result.started_at
    record.timeout_at = None
    record.finished_at = result.finished_at
    record.result_digest = result_digest
    record.result_document = result_document
    failed = next(
        (
            case_result.error_message
            for case_result in result.case_results
            if case_result.status in {CaseResultStatus.FAILED, CaseResultStatus.INFRA_ERROR}
            and case_result.error_message
        ),
        None,
    )
    record.error_message = failed
    await _release_runner_slot(session, run_id)
    validation_request = await session.scalar(
        select(ChangeRequestRecord).where(ChangeRequestRecord.validation_run_id == run_id)
    )
    if validation_request is not None:
        validation_request.validation_status = result.status.value
    await _append_run_event(
        session,
        record,
        dedupe_key=f"system:result:{result_digest}",
        source="SYSTEM",
        event_type="result_recorded",
        occurred_at=result.finished_at,
        status=result.status.value,
        payload={
            "result_digest": result_digest,
            "artifact_count": len(result.artifacts),
        },
    )
    session.add(
        _audit(
            actor_id=RUNNER_ACTOR_ID,
            action="run.result_recorded",
            resource_type="test_run",
            resource_id=run_id,
            project_id=record.project_id,
            details={
                "status": result.status.value,
                "result_digest": result_digest,
                "artifact_count": len(result.artifacts),
            },
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        existing = await get_run(session, run_id)
        if existing.result_digest == result_digest:
            return existing, False
        raise ResourceConflict("Run Result artifacts conflict with existing records") from exc
    await session.refresh(record)
    return record, True


async def list_runs(
    session: AsyncSession,
    project_id: UUID,
    *,
    statuses: tuple[RunStatus, ...] = (),
    target_id: UUID | None = None,
    environment_id: UUID | None = None,
    created_by: UUID | None = None,
    source_run_id: UUID | None = None,
    case_code: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int,
    offset: int,
) -> tuple[tuple[TestRunRecord, ...], int]:
    await _project(session, project_id)
    conditions = [TestRunRecord.project_id == project_id]
    if statuses:
        conditions.append(TestRunRecord.status.in_(status.value for status in statuses))
    if target_id is not None:
        conditions.append(TestRunRecord.target_id == target_id)
    if environment_id is not None:
        conditions.append(TestRunRecord.environment_id == environment_id)
    if created_by is not None:
        conditions.append(TestRunRecord.created_by == created_by)
    if source_run_id is not None:
        conditions.append(TestRunRecord.source_run_id == source_run_id)
    if case_code:
        conditions.append(
            TestRunRecord.id.in_(
                select(RunCaseRecord.run_id).where(RunCaseRecord.case_code == case_code)
            )
        )
    if created_from is not None:
        conditions.append(TestRunRecord.created_at >= created_from)
    if created_to is not None:
        conditions.append(TestRunRecord.created_at <= created_to)
    total = await session.scalar(select(func.count()).select_from(TestRunRecord).where(*conditions))
    result = await session.scalars(
        select(TestRunRecord)
        .where(*conditions)
        .order_by(TestRunRecord.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return tuple(result), int(total or 0)


async def _request_cancel(
    session: AsyncSession,
    record: TestRunRecord,
    actor_id: UUID,
) -> bool:
    current = RunStatus(record.status)
    if current in TERMINAL_RUN_STATUSES or record.cancel_requested:
        return False
    record.cancel_requested = True
    if current == RunStatus.QUEUED:
        require_run_transition(current, RunStatus.CANCELED)
        record.status = RunStatus.CANCELED.value
        record.finished_at = utc_now()
        await _release_runner_slot(session, record.id)
    await _append_run_event(
        session,
        record,
        dedupe_key="system:cancel_requested",
        source="SYSTEM",
        event_type="cancel_requested",
        occurred_at=utc_now(),
        status=record.status,
        payload={"previous_status": current.value, "status": record.status},
    )
    session.add(
        DispatchOutboxRecord(
            aggregate_type="test_run",
            aggregate_id=record.id,
            event_type="run.cancel_requested",
            dedupe_key=f"run.cancel_requested:{record.id}",
            payload={"run_id": str(record.id)},
        )
    )
    session.add(
        _audit(
            actor_id=actor_id,
            action="run.cancel_requested",
            resource_type="test_run",
            resource_id=record.id,
            project_id=record.project_id,
            details={"previous_status": current.value, "status": record.status},
        )
    )
    return True


async def cancel_run(
    session: AsyncSession,
    run_id: UUID,
    actor_id: UUID,
) -> TestRunRecord:
    record = await _locked_run(session, run_id)
    changed = await _request_cancel(session, record, actor_id)
    if not changed:
        return record
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return await get_run(session, run_id)
    await session.refresh(record)
    return record


async def cancel_runs(
    session: AsyncSession,
    project_id: UUID,
    run_ids: tuple[UUID, ...],
    actor_id: UUID,
) -> tuple[tuple[TestRunRecord, ...], int]:
    await _project(session, project_id)
    records = tuple(
        await session.scalars(
            select(TestRunRecord).where(TestRunRecord.id.in_(run_ids)).with_for_update()
        )
    )
    by_id = {record.id: record for record in records if record.project_id == project_id}
    if len(by_id) != len(run_ids):
        raise ResourceNotFound("one or more Runs were not found in project")
    ordered = tuple(by_id[run_id] for run_id in run_ids)
    changed = 0
    for record in ordered:
        if await _request_cancel(session, record, actor_id):
            changed += 1
    if changed:
        await session.commit()
        for record in ordered:
            await session.refresh(record)
    return ordered, changed
