"""Recover stalled Runs and expose low-cardinality reliability snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from testops.contracts import RunStatus

from .persistence import (
    AuditLogRecord,
    ChangeRequestRecord,
    DispatchOutboxRecord,
    RegressionScheduleRecord,
    RunCaseRecord,
    RunEventRecord,
    RunnerSlotLeaseRecord,
    RunnerWorkerRecord,
    TestRunRecord,
)
from .state_machine import require_run_transition

SYSTEM_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
TERMINAL_STATUS_VALUES = frozenset(
    {
        RunStatus.PASSED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELED.value,
        RunStatus.TIMED_OUT.value,
        RunStatus.INFRA_ERROR.value,
    }
)
IN_FLIGHT_STATUS_VALUES = (
    RunStatus.QUEUED.value,
    RunStatus.PREPARING.value,
    RunStatus.RUNNING.value,
)


@dataclass(frozen=True, slots=True)
class ReliabilitySweepSummary:
    selected: int
    timed_out: int
    runner_lost: int
    dispatch_stalled: int
    leases_released: int


@dataclass(frozen=True, slots=True)
class ReliabilitySnapshot:
    queued_runs: int
    preparing_runs: int
    running_runs: int
    dispatch_waiting_runs: int
    dispatch_backlog_oldest_age_seconds: float
    due_schedules: int
    schedule_lag_seconds: float
    stale_runner_leases: int


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _append_reliability_event(
    session: AsyncSession,
    run: TestRunRecord,
    *,
    reason_code: str,
    target: RunStatus,
    moment: datetime,
    details: dict[str, object],
) -> None:
    current_sequence = await session.scalar(
        select(func.max(RunEventRecord.sequence)).where(RunEventRecord.run_id == run.id)
    )
    session.add(
        RunEventRecord(
            run_id=run.id,
            sequence=int(current_sequence or 0) + 1,
            dedupe_key=f"system:reliability:{reason_code}",
            source="SYSTEM",
            event_type="run_recovered",
            status=target.value,
            payload={"reason": reason_code, **details},
            occurred_at=moment,
        )
    )


async def _active_lease(
    session: AsyncSession,
    run_id: UUID,
) -> RunnerSlotLeaseRecord | None:
    return await session.scalar(
        select(RunnerSlotLeaseRecord)
        .where(
            RunnerSlotLeaseRecord.run_id == run_id,
            RunnerSlotLeaseRecord.status == "ACTIVE",
        )
        .with_for_update()
    )


async def _recover_run(
    session: AsyncSession,
    run: TestRunRecord,
    *,
    target: RunStatus,
    reason_code: str,
    message: str,
    moment: datetime,
    details: dict[str, object] | None = None,
) -> bool:
    current = RunStatus(run.status)
    if current.value in TERMINAL_STATUS_VALUES:
        return False
    require_run_transition(current, target)
    recovery_details = {
        "previous_status": current.value,
        "status": target.value,
        **(details or {}),
    }
    run.status = target.value
    run.cancel_requested = True
    run.timeout_at = None
    run.finished_at = moment
    run.error_message = message
    run.dispatch_wait_reason = None
    lease = await _active_lease(session, run.id)
    if lease is not None:
        lease.status = "EXPIRED"
        lease.released_at = moment
    await session.execute(
        update(RunCaseRecord)
        .where(
            RunCaseRecord.run_id == run.id,
            RunCaseRecord.status.in_(IN_FLIGHT_STATUS_VALUES),
        )
        .values(status=target.value, error_message=message)
    )
    validation_request = await session.scalar(
        select(ChangeRequestRecord).where(ChangeRequestRecord.validation_run_id == run.id)
    )
    if validation_request is not None:
        validation_request.validation_status = target.value
    await _append_reliability_event(
        session,
        run,
        reason_code=reason_code,
        target=target,
        moment=moment,
        details=recovery_details,
    )
    session.add(
        DispatchOutboxRecord(
            id=uuid4(),
            aggregate_type="test_run",
            aggregate_id=run.id,
            event_type="run.cancel_requested",
            dedupe_key=f"run.reliability_cancel:{reason_code}:{run.id}",
            payload={"run_id": str(run.id), "reason": reason_code},
            available_at=moment,
        )
    )
    session.add(
        AuditLogRecord(
            actor_id=SYSTEM_ACTOR_ID,
            action=f"run.{reason_code}",
            resource_type="test_run",
            resource_id=str(run.id),
            project_id=run.project_id,
            details=recovery_details,
            created_at=moment,
        )
    )
    return lease is not None


async def process_run_reliability(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    batch_size: int = 100,
    heartbeat_ttl_seconds: int = 45,
    dispatch_start_timeout_seconds: int = 300,
) -> ReliabilitySweepSummary:
    if not 1 <= batch_size <= 1000:
        raise ValueError("reliability batch size must be between 1 and 1000")
    if not 15 <= heartbeat_ttl_seconds <= 600:
        raise ValueError("Runner heartbeat TTL must be between 15 and 600 seconds")
    if not 30 <= dispatch_start_timeout_seconds <= 3600:
        raise ValueError("dispatch start timeout must be between 30 and 3600 seconds")
    moment = now or datetime.now(UTC)
    stale_before = moment - timedelta(seconds=heartbeat_ttl_seconds)
    dispatch_before = moment - timedelta(seconds=dispatch_start_timeout_seconds)
    selected = 0
    timed_out = 0
    runner_lost = 0
    dispatch_stalled = 0
    leases_released = 0

    async with session_factory() as session:
        async with session.begin():
            timeout_runs = tuple(
                await session.scalars(
                    select(TestRunRecord)
                    .where(
                        TestRunRecord.status.in_(
                            (RunStatus.PREPARING.value, RunStatus.RUNNING.value)
                        ),
                        TestRunRecord.timeout_at.is_not(None),
                        TestRunRecord.timeout_at <= moment,
                    )
                    .order_by(TestRunRecord.timeout_at, TestRunRecord.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            for run in timeout_runs:
                released = await _recover_run(
                    session,
                    run,
                    target=RunStatus.TIMED_OUT,
                    reason_code="timed_out",
                    message=f"Run exceeded its {run.timeout_seconds}-second execution timeout",
                    moment=moment,
                    details={"timeout_seconds": run.timeout_seconds},
                )
                selected += 1
                timed_out += 1
                leases_released += int(released)

            remaining = batch_size - selected
            if remaining:
                stale_rows = tuple(
                    (
                        await session.execute(
                            select(
                                RunnerSlotLeaseRecord,
                                TestRunRecord,
                                RunnerWorkerRecord,
                            )
                            .join(
                                TestRunRecord,
                                TestRunRecord.id == RunnerSlotLeaseRecord.run_id,
                            )
                            .join(
                                RunnerWorkerRecord,
                                RunnerWorkerRecord.id == RunnerSlotLeaseRecord.worker_id,
                            )
                            .where(
                                RunnerSlotLeaseRecord.status == "ACTIVE",
                                TestRunRecord.status.in_(
                                    (RunStatus.PREPARING.value, RunStatus.RUNNING.value)
                                ),
                                RunnerWorkerRecord.last_heartbeat_at < stale_before,
                            )
                            .order_by(
                                RunnerWorkerRecord.last_heartbeat_at,
                                RunnerSlotLeaseRecord.acquired_at,
                            )
                            .limit(remaining)
                            .with_for_update(skip_locked=True)
                        )
                    ).all()
                )
                for _lease, run, worker in stale_rows:
                    released = await _recover_run(
                        session,
                        run,
                        target=RunStatus.INFRA_ERROR,
                        reason_code="runner_lost",
                        message=f"Runner worker {worker.worker_key} heartbeat expired",
                        moment=moment,
                        details={
                            "worker_key": worker.worker_key,
                            "last_heartbeat_at": _aware(worker.last_heartbeat_at).isoformat(),
                        },
                    )
                    selected += 1
                    runner_lost += 1
                    leases_released += int(released)

            remaining = batch_size - selected
            if remaining:
                stalled_rows = tuple(
                    (
                        await session.execute(
                            select(RunnerSlotLeaseRecord, TestRunRecord)
                            .join(
                                TestRunRecord,
                                TestRunRecord.id == RunnerSlotLeaseRecord.run_id,
                            )
                            .where(
                                RunnerSlotLeaseRecord.status == "ACTIVE",
                                RunnerSlotLeaseRecord.worker_id.is_(None),
                                TestRunRecord.status == RunStatus.QUEUED.value,
                                TestRunRecord.dispatched_at.is_not(None),
                                TestRunRecord.dispatched_at <= dispatch_before,
                            )
                            .order_by(TestRunRecord.dispatched_at, TestRunRecord.id)
                            .limit(remaining)
                            .with_for_update(skip_locked=True)
                        )
                    ).all()
                )
                for _lease, run in stalled_rows:
                    released = await _recover_run(
                        session,
                        run,
                        target=RunStatus.INFRA_ERROR,
                        reason_code="dispatch_stalled",
                        message=(
                            "Dispatched Run was not claimed by a Runner before the start timeout"
                        ),
                        moment=moment,
                        details={
                            "dispatch_start_timeout_seconds": dispatch_start_timeout_seconds,
                        },
                    )
                    selected += 1
                    dispatch_stalled += 1
                    leases_released += int(released)

            remaining = batch_size - selected
            if remaining:
                orphaned_leases = tuple(
                    await session.scalars(
                        select(RunnerSlotLeaseRecord)
                        .join(TestRunRecord, TestRunRecord.id == RunnerSlotLeaseRecord.run_id)
                        .where(
                            RunnerSlotLeaseRecord.status == "ACTIVE",
                            TestRunRecord.status.in_(TERMINAL_STATUS_VALUES),
                        )
                        .order_by(RunnerSlotLeaseRecord.acquired_at)
                        .limit(remaining)
                        .with_for_update(skip_locked=True)
                    )
                )
                for lease in orphaned_leases:
                    lease.status = "RELEASED"
                    lease.released_at = moment
                    selected += 1
                    leases_released += 1

    return ReliabilitySweepSummary(
        selected=selected,
        timed_out=timed_out,
        runner_lost=runner_lost,
        dispatch_stalled=dispatch_stalled,
        leases_released=leases_released,
    )


async def collect_reliability_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    heartbeat_ttl_seconds: int = 45,
) -> ReliabilitySnapshot:
    if not 15 <= heartbeat_ttl_seconds <= 600:
        raise ValueError("Runner heartbeat TTL must be between 15 and 600 seconds")
    moment = now or datetime.now(UTC)
    stale_before = moment - timedelta(seconds=heartbeat_ttl_seconds)
    async with session_factory() as session:
        status_rows = await session.execute(
            select(TestRunRecord.status, func.count())
            .where(TestRunRecord.status.in_(IN_FLIGHT_STATUS_VALUES))
            .group_by(TestRunRecord.status)
        )
        status_counts = {status: int(count) for status, count in status_rows}
        dispatch_waiting, oldest_dispatch = (
            await session.execute(
                select(func.count(), func.min(TestRunRecord.created_at)).where(
                    TestRunRecord.status == RunStatus.QUEUED.value,
                    TestRunRecord.dispatch_state == "WAITING",
                )
            )
        ).one()
        due_schedules, oldest_schedule = (
            await session.execute(
                select(func.count(), func.min(RegressionScheduleRecord.next_fire_at)).where(
                    RegressionScheduleRecord.status == "ACTIVE",
                    RegressionScheduleRecord.next_fire_at.is_not(None),
                    RegressionScheduleRecord.next_fire_at <= moment,
                )
            )
        ).one()
        stale_runner_leases = await session.scalar(
            select(func.count())
            .select_from(RunnerSlotLeaseRecord)
            .join(TestRunRecord, TestRunRecord.id == RunnerSlotLeaseRecord.run_id)
            .join(
                RunnerWorkerRecord,
                RunnerWorkerRecord.id == RunnerSlotLeaseRecord.worker_id,
            )
            .where(
                RunnerSlotLeaseRecord.status == "ACTIVE",
                TestRunRecord.status.in_((RunStatus.PREPARING.value, RunStatus.RUNNING.value)),
                RunnerWorkerRecord.last_heartbeat_at < stale_before,
            )
        )
    dispatch_age = (
        max(0.0, (moment - _aware(oldest_dispatch)).total_seconds())
        if oldest_dispatch is not None
        else 0.0
    )
    schedule_lag = (
        max(0.0, (moment - _aware(oldest_schedule)).total_seconds())
        if oldest_schedule is not None
        else 0.0
    )
    return ReliabilitySnapshot(
        queued_runs=status_counts.get(RunStatus.QUEUED.value, 0),
        preparing_runs=status_counts.get(RunStatus.PREPARING.value, 0),
        running_runs=status_counts.get(RunStatus.RUNNING.value, 0),
        dispatch_waiting_runs=int(dispatch_waiting or 0),
        dispatch_backlog_oldest_age_seconds=dispatch_age,
        due_schedules=int(due_schedules or 0),
        schedule_lag_seconds=schedule_lag,
        stale_runner_leases=int(stale_runner_leases or 0),
    )
