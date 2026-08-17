"""Regression schedule lifecycle and deterministic due-schedule processing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from testops.contracts import CaseBaseline

from .cron import CronExpression
from .persistence import (
    AutomationPackageRecord,
    CaseBaselineRecord,
    EnvironmentRecord,
    ProjectRecord,
    RegressionScheduleFiringRecord,
    RegressionScheduleRecord,
    TestRunRecord,
    TestTargetRecord,
    utc_now,
)
from .schemas import RegressionScheduleCreate, RegressionScheduleUpdate, RunCreate
from .services import (
    InvalidRequest,
    ResourceConflict,
    ResourceNotFound,
    ServiceError,
    _audit,
    _commit_unique,
    _select_cases,
    create_run,
)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _schedule(
    session: AsyncSession,
    project_id: UUID,
    schedule_id: UUID,
    *,
    lock: bool = False,
) -> RegressionScheduleRecord:
    statement = select(RegressionScheduleRecord).where(
        RegressionScheduleRecord.id == schedule_id,
        RegressionScheduleRecord.project_id == project_id,
    )
    if lock:
        statement = statement.with_for_update()
    record = await session.scalar(statement)
    if record is None:
        raise ResourceNotFound("regression schedule not found")
    return record


async def _validate_resources(
    session: AsyncSession,
    *,
    project_id: UUID,
    target_id: UUID,
    environment_id: UUID,
    baseline_id: UUID,
    automation_package_id: UUID,
    case_codes: tuple[str, ...],
) -> None:
    project = await session.get(ProjectRecord, project_id)
    target = await session.get(TestTargetRecord, target_id)
    environment = await session.get(EnvironmentRecord, environment_id)
    baseline_record = await session.get(CaseBaselineRecord, baseline_id)
    package = await session.get(AutomationPackageRecord, automation_package_id)
    if project is None:
        raise ResourceNotFound("project not found")
    if project.status != "ACTIVE":
        raise ResourceConflict("regression schedules require an ACTIVE project")
    if target is None or target.project_id != project_id:
        raise ResourceNotFound("target not found in project")
    if target.status != "ACTIVE":
        raise ResourceConflict("regression schedules require an ACTIVE target")
    if (
        environment is None
        or environment.project_id != project_id
        or environment.target_id != target_id
    ):
        raise ResourceNotFound("environment not found for target")
    if environment.status != "ACTIVE":
        raise ResourceConflict("regression schedules require an ACTIVE environment")
    if baseline_record is None or baseline_record.project_id != project_id:
        raise ResourceNotFound("baseline not found in project")
    if baseline_record.status != "RELEASED":
        raise ResourceConflict("regression schedules require a RELEASED baseline")
    if package is None or package.project_id != project_id or package.target_id != target_id:
        raise ResourceNotFound("automation package not found for target")
    if package.status != "ACTIVE":
        raise ResourceConflict("regression schedules require an ACTIVE automation package")
    baseline = CaseBaseline.model_validate(baseline_record.document)
    if not _select_cases(baseline, case_codes):
        raise InvalidRequest("regression schedule must contain at least one enabled case")


async def create_regression_schedule(
    session: AsyncSession,
    project_id: UUID,
    payload: RegressionScheduleCreate,
    actor_id: UUID,
    *,
    now: datetime | None = None,
) -> RegressionScheduleRecord:
    await _validate_resources(
        session,
        project_id=project_id,
        target_id=payload.target_id,
        environment_id=payload.environment_id,
        baseline_id=payload.baseline_id,
        automation_package_id=payload.automation_package_id,
        case_codes=payload.case_codes,
    )
    moment = (now or utc_now()).astimezone(UTC)
    next_fire_at = (
        CronExpression.parse(payload.cron_expression).next_after(moment, payload.timezone)
        if payload.status == "ACTIVE"
        else None
    )
    record = RegressionScheduleRecord(
        id=uuid4(),
        project_id=project_id,
        key=payload.key,
        name=payload.name,
        description=payload.description,
        target_id=payload.target_id,
        environment_id=payload.environment_id,
        baseline_id=payload.baseline_id,
        automation_package_id=payload.automation_package_id,
        case_codes=list(payload.case_codes),
        cron_expression=payload.cron_expression,
        timezone=payload.timezone,
        misfire_policy=payload.misfire_policy,
        misfire_grace_seconds=payload.misfire_grace_seconds,
        status=payload.status,
        next_fire_at=next_fire_at,
        created_by=actor_id,
    )
    session.add(record)
    session.add(
        _audit(
            actor_id=actor_id,
            action="regression_schedule.created",
            resource_type="regression_schedule",
            resource_id=record.id,
            project_id=project_id,
            details={
                "key": record.key,
                "cron_expression": record.cron_expression,
                "timezone": record.timezone,
                "status": record.status,
                "next_fire_at": next_fire_at.isoformat() if next_fire_at else None,
            },
        )
    )
    await _commit_unique(session, "regression schedule key already exists in project")
    await session.refresh(record)
    return record


async def list_regression_schedules(
    session: AsyncSession,
    project_id: UUID,
) -> tuple[RegressionScheduleRecord, ...]:
    project = await session.get(ProjectRecord, project_id)
    if project is None:
        raise ResourceNotFound("project not found")
    return tuple(
        await session.scalars(
            select(RegressionScheduleRecord)
            .where(RegressionScheduleRecord.project_id == project_id)
            .order_by(RegressionScheduleRecord.created_at)
        )
    )


async def update_regression_schedule(
    session: AsyncSession,
    project_id: UUID,
    schedule_id: UUID,
    payload: RegressionScheduleUpdate,
    actor_id: UUID,
    *,
    now: datetime | None = None,
) -> RegressionScheduleRecord:
    record = await _schedule(session, project_id, schedule_id, lock=True)
    if record.status == "ARCHIVED" and payload.status not in {None, "ARCHIVED"}:
        raise ResourceConflict("an archived regression schedule cannot be reactivated")
    next_values = {
        "target_id": payload.target_id or record.target_id,
        "environment_id": payload.environment_id or record.environment_id,
        "baseline_id": payload.baseline_id or record.baseline_id,
        "automation_package_id": payload.automation_package_id or record.automation_package_id,
        "case_codes": payload.case_codes
        if payload.case_codes is not None
        else tuple(record.case_codes),
    }
    resource_fields = {
        "target_id",
        "environment_id",
        "baseline_id",
        "automation_package_id",
        "case_codes",
    }
    if payload.model_fields_set.intersection(resource_fields):
        await _validate_resources(session, project_id=project_id, **next_values)
    before = {
        "name": record.name,
        "description": record.description,
        "cron_expression": record.cron_expression,
        "timezone": record.timezone,
        "misfire_policy": record.misfire_policy,
        "misfire_grace_seconds": record.misfire_grace_seconds,
        "status": record.status,
        "next_fire_at": record.next_fire_at.isoformat() if record.next_fire_at else None,
    }
    for field in (
        "name",
        "target_id",
        "environment_id",
        "baseline_id",
        "automation_package_id",
        "cron_expression",
        "timezone",
        "misfire_policy",
        "misfire_grace_seconds",
        "status",
    ):
        value = getattr(payload, field)
        if value is not None:
            setattr(record, field, value)
    if "description" in payload.model_fields_set:
        record.description = payload.description
    if payload.case_codes is not None:
        record.case_codes = list(payload.case_codes)

    schedule_fields = {"cron_expression", "timezone", "status"}
    if record.status == "ACTIVE" and (
        record.next_fire_at is None or payload.model_fields_set.intersection(schedule_fields)
    ):
        moment = (now or utc_now()).astimezone(UTC)
        record.next_fire_at = CronExpression.parse(record.cron_expression).next_after(
            moment, record.timezone
        )
    elif record.status != "ACTIVE":
        record.next_fire_at = None
    after = {
        "name": record.name,
        "description": record.description,
        "cron_expression": record.cron_expression,
        "timezone": record.timezone,
        "misfire_policy": record.misfire_policy,
        "misfire_grace_seconds": record.misfire_grace_seconds,
        "status": record.status,
        "next_fire_at": record.next_fire_at.isoformat() if record.next_fire_at else None,
    }
    session.add(
        _audit(
            actor_id=actor_id,
            action="regression_schedule.updated",
            resource_type="regression_schedule",
            resource_id=record.id,
            project_id=project_id,
            details={"before": before, "after": after},
        )
    )
    await session.commit()
    await session.refresh(record)
    return record


async def list_regression_schedule_firings(
    session: AsyncSession,
    project_id: UUID,
    schedule_id: UUID,
    *,
    limit: int = 50,
) -> tuple[RegressionScheduleFiringRecord, ...]:
    await _schedule(session, project_id, schedule_id)
    return tuple(
        await session.scalars(
            select(RegressionScheduleFiringRecord)
            .where(RegressionScheduleFiringRecord.schedule_id == schedule_id)
            .order_by(RegressionScheduleFiringRecord.created_at.desc())
            .limit(limit)
        )
    )


def _run_payload(record: RegressionScheduleRecord) -> RunCreate:
    return RunCreate(
        project_id=record.project_id,
        target_id=record.target_id,
        environment_id=record.environment_id,
        baseline_id=record.baseline_id,
        automation_package_id=record.automation_package_id,
        case_codes=tuple(record.case_codes),
    )


def _scheduled_idempotency_key(schedule_id: UUID, scheduled_for: datetime) -> str:
    timestamp = scheduled_for.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"schedule:{schedule_id}:{timestamp}"


def _manual_idempotency_key(schedule_id: UUID, idempotency_key: str) -> str:
    digest = sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"schedule:manual:{schedule_id}:{digest}"


async def trigger_regression_schedule(
    session: AsyncSession,
    project_id: UUID,
    schedule_id: UUID,
    *,
    idempotency_key: str,
    actor_id: UUID,
    now: datetime | None = None,
) -> tuple[TestRunRecord, bool]:
    record = await _schedule(session, project_id, schedule_id, lock=True)
    if record.status == "ARCHIVED":
        raise ResourceConflict("an archived regression schedule cannot be triggered")
    await _validate_resources(
        session,
        project_id=project_id,
        target_id=record.target_id,
        environment_id=record.environment_id,
        baseline_id=record.baseline_id,
        automation_package_id=record.automation_package_id,
        case_codes=tuple(record.case_codes),
    )
    moment = (now or utc_now()).astimezone(UTC)
    run, created = await create_run(
        session,
        _run_payload(record),
        idempotency_key=_manual_idempotency_key(record.id, idempotency_key),
        actor_id=actor_id,
        regression_schedule_id=record.id,
        scheduled_for=moment,
        commit=False,
    )
    firing = await session.scalar(
        select(RegressionScheduleFiringRecord).where(
            RegressionScheduleFiringRecord.schedule_id == record.id,
            RegressionScheduleFiringRecord.run_id == run.id,
        )
    )
    if firing is None:
        firing = RegressionScheduleFiringRecord(
            id=uuid4(),
            schedule_id=record.id,
            run_id=run.id,
            scheduled_for=moment,
            triggered_at=moment,
            trigger_kind="MANUAL",
            status="TRIGGERED",
        )
        session.add(firing)
        session.add(
            _audit(
                actor_id=actor_id,
                action="regression_schedule.triggered_manually",
                resource_type="regression_schedule",
                resource_id=record.id,
                project_id=project_id,
                details={"run_id": str(run.id)},
            )
        )
    record.last_scheduled_for = _aware(firing.scheduled_for).astimezone(UTC)
    record.last_triggered_at = (
        _aware(firing.triggered_at).astimezone(UTC) if firing.triggered_at else None
    )
    record.last_run_id = run.id
    record.last_error = None
    await session.commit()
    await session.refresh(run)
    return run, created


@dataclass(frozen=True, slots=True)
class ScheduleDispatchSummary:
    selected: int
    triggered: int
    skipped: int
    blocked: int
    failed: int


async def _process_due_schedule(
    session_factory: async_sessionmaker[AsyncSession],
    schedule_id: UUID,
    *,
    moment: datetime,
) -> str:
    async with session_factory() as session:
        record = await session.scalar(
            select(RegressionScheduleRecord)
            .where(RegressionScheduleRecord.id == schedule_id)
            .with_for_update()
        )
        if (
            record is None
            or record.status != "ACTIVE"
            or record.next_fire_at is None
            or _aware(record.next_fire_at) > moment
        ):
            return "ignored"
        scheduled_for = _aware(record.next_fire_at).astimezone(UTC)
        cron = CronExpression.parse(record.cron_expression)
        next_fire_at = cron.next_after(moment, record.timezone)
        existing = await session.scalar(
            select(RegressionScheduleFiringRecord).where(
                RegressionScheduleFiringRecord.schedule_id == record.id,
                RegressionScheduleFiringRecord.scheduled_for == scheduled_for,
            )
        )
        if existing is not None:
            record.next_fire_at = next_fire_at
            await session.commit()
            return "ignored"
        missed = moment - scheduled_for > timedelta(seconds=record.misfire_grace_seconds)
        trigger_kind = "MISFIRE" if missed else "SCHEDULED"
        firing = RegressionScheduleFiringRecord(
            id=uuid4(),
            schedule_id=record.id,
            scheduled_for=scheduled_for,
            triggered_at=moment,
            trigger_kind=trigger_kind,
            status="SKIPPED" if missed and record.misfire_policy == "SKIP" else "TRIGGERED",
        )
        session.add(firing)
        record.next_fire_at = next_fire_at
        record.last_scheduled_for = scheduled_for
        if missed and record.misfire_policy == "SKIP":
            record.last_error = "MISFIRE_SKIPPED"
            session.add(
                _audit(
                    actor_id=record.created_by,
                    action="regression_schedule.misfire_skipped",
                    resource_type="regression_schedule",
                    resource_id=record.id,
                    project_id=record.project_id,
                    details={
                        "scheduled_for": scheduled_for.isoformat(),
                        "next_fire_at": next_fire_at.isoformat(),
                    },
                )
            )
            await session.commit()
            return "skipped"
        try:
            await _validate_resources(
                session,
                project_id=record.project_id,
                target_id=record.target_id,
                environment_id=record.environment_id,
                baseline_id=record.baseline_id,
                automation_package_id=record.automation_package_id,
                case_codes=tuple(record.case_codes),
            )
            run, _ = await create_run(
                session,
                _run_payload(record),
                idempotency_key=_scheduled_idempotency_key(record.id, scheduled_for),
                actor_id=record.created_by,
                regression_schedule_id=record.id,
                scheduled_for=scheduled_for,
                commit=False,
            )
        except ServiceError as exc:
            firing.status = "BLOCKED"
            firing.error_message = str(exc)[:2000]
            record.last_triggered_at = moment
            record.last_error = str(exc)[:2000]
            session.add(
                _audit(
                    actor_id=record.created_by,
                    action="regression_schedule.trigger_blocked",
                    resource_type="regression_schedule",
                    resource_id=record.id,
                    project_id=record.project_id,
                    details={
                        "scheduled_for": scheduled_for.isoformat(),
                        "error": record.last_error,
                    },
                )
            )
            await session.commit()
            return "blocked"
        firing.run_id = run.id
        record.last_triggered_at = moment
        record.last_run_id = run.id
        record.last_error = None
        session.add(
            _audit(
                actor_id=record.created_by,
                action="regression_schedule.triggered",
                resource_type="regression_schedule",
                resource_id=record.id,
                project_id=record.project_id,
                details={
                    "run_id": str(run.id),
                    "scheduled_for": scheduled_for.isoformat(),
                    "trigger_kind": trigger_kind,
                },
            )
        )
        await session.commit()
        return "triggered"


async def process_due_schedules(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    limit: int = 50,
    now: datetime | None = None,
) -> ScheduleDispatchSummary:
    if not 1 <= limit <= 500:
        raise ValueError("schedule batch limit must be between 1 and 500")
    moment = (now or utc_now()).astimezone(UTC)
    async with session_factory() as session:
        schedule_ids = tuple(
            await session.scalars(
                select(RegressionScheduleRecord.id)
                .where(
                    RegressionScheduleRecord.status == "ACTIVE",
                    RegressionScheduleRecord.next_fire_at.is_not(None),
                    RegressionScheduleRecord.next_fire_at <= moment,
                )
                .order_by(RegressionScheduleRecord.next_fire_at)
                .limit(limit)
            )
        )
    counts = {"triggered": 0, "skipped": 0, "blocked": 0, "failed": 0}
    for schedule_id in schedule_ids:
        try:
            outcome = await _process_due_schedule(
                session_factory,
                schedule_id,
                moment=moment,
            )
        except Exception:
            counts["failed"] += 1
        else:
            if outcome in counts:
                counts[outcome] += 1
    return ScheduleDispatchSummary(selected=len(schedule_ids), **counts)
