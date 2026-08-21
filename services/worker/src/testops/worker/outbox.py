"""Transactional Outbox publisher for durable Celery dispatch."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from testops.api.persistence import (
    DispatchOutboxRecord,
    RunnerPoolRecord,
    RunnerSlotLeaseRecord,
    RunnerWorkerRecord,
    TestRunRecord,
)
from testops.contracts import RunStatus

TERMINAL_STATUS_VALUES = frozenset(
    {
        RunStatus.PASSED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELED.value,
        RunStatus.TIMED_OUT.value,
        RunStatus.INFRA_ERROR.value,
    }
)


class TaskPublisher(Protocol):
    def send_task(
        self,
        name: str,
        *,
        args: list[object],
        task_id: str,
        queue: str | None = None,
    ) -> object:
        """Publish one idempotently identified task."""


@dataclass(frozen=True, slots=True)
class DispatchSummary:
    selected: int
    published: int
    failed: int
    waiting: int


@dataclass(frozen=True, slots=True)
class PreparedDispatch:
    queue: str | None
    run: TestRunRecord | None
    lease: RunnerSlotLeaseRecord | None
    skip_publish: bool = False


class CapacityWait(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _task_for(record: DispatchOutboxRecord) -> tuple[str, list[object], str]:
    if record.event_type == "run.queued":
        return (
            "testops.execute_run",
            [record.payload["run_snapshot"]],
            f"run:{record.aggregate_id}",
        )
    if record.event_type == "run.cancel_requested":
        return (
            "testops.cancel_run",
            [str(record.aggregate_id)],
            f"cancel:{record.id}",
        )
    raise ValueError(f"unsupported Outbox event: {record.event_type}")


def _worker_matches(
    worker: RunnerWorkerRecord,
    *,
    target_type: str,
    browser: str | None,
) -> bool:
    capabilities = worker.capabilities
    if target_type not in capabilities.get("target_types", []):
        return False
    if browser is not None and browser not in capabilities.get("browsers", []):
        return False
    return True


def _worker_hosts_package(
    worker: RunnerWorkerRecord,
    *,
    runner_type: str,
    image_repository: str,
    digest: str,
) -> bool:
    advertised = worker.capabilities.get("automation_packages", [])
    if not isinstance(advertised, list):
        return False
    requested_digest = digest.lower()
    return any(
        isinstance(package, dict)
        and package.get("runner_type") == runner_type
        and package.get("image_repository") == image_repository
        and str(package.get("digest", "")).lower() == requested_digest
        for package in advertised
    )


def _worker_supports_execution_isolation(worker: RunnerWorkerRecord) -> bool:
    isolation = worker.capabilities.get("execution_isolation")
    if not isinstance(isolation, dict):
        return False
    if isolation.get("mode") == "SUBPROCESS":
        return (
            isolation.get("dedicated_process") is True
            and isolation.get("credential_scope") == "RUN_SECRETS_ONLY"
        )
    mode = isolation.get("mode")
    if mode not in {"CONTAINER", "KUBERNETES"}:
        return False
    hard_isolation = (
        isolation.get("dedicated_process") is True
        and isolation.get("credential_scope") == "RUN_SECRETS_ONLY"
        and isolation.get("read_only_root_filesystem") is True
        and isolation.get("network_policy") in {"DENY_ALL", "ALLOWLIST"}
        and isolation.get("resource_limits_enforced") is True
        and isinstance(isolation.get("memory_limit_bytes"), int)
        and isinstance(isolation.get("cpu_limit_millis"), int)
    )
    if not hard_isolation:
        return False
    if mode == "CONTAINER":
        return isinstance(isolation.get("pids_limit"), int)
    return (
        isinstance(isolation.get("ephemeral_storage_limit_bytes"), int)
        and isinstance(isolation.get("orchestrator_namespace"), str)
        and isolation.get("service_account_name") not in {None, "", "default"}
        and isolation.get("service_account_token_automounted") is False
    )


def _worker_isolation_rank(worker: RunnerWorkerRecord) -> int:
    isolation = worker.capabilities.get("execution_isolation")
    if not isinstance(isolation, dict):
        return 0
    return {"SUBPROCESS": 1, "CONTAINER": 2, "KUBERNETES": 3}.get(str(isolation.get("mode")), 0)


async def _prepare_run_dispatch(
    session: AsyncSession,
    record: DispatchOutboxRecord,
    *,
    dispatch_time: datetime,
    heartbeat_ttl_seconds: int,
) -> PreparedDispatch:
    if record.event_type != "run.queued":
        return PreparedDispatch(queue=None, run=None, lease=None)
    run = await session.scalar(
        select(TestRunRecord).where(TestRunRecord.id == record.aggregate_id).with_for_update()
    )
    if run is None:
        raise ValueError("Outbox Run no longer exists")
    if run.status in TERMINAL_STATUS_VALUES:
        run.dispatch_state = "DISPATCHED"
        run.dispatch_wait_reason = None
        run.dispatched_at = dispatch_time
        return PreparedDispatch(queue=None, run=run, lease=None, skip_publish=True)
    if run.runner_pool_id is None:
        return PreparedDispatch(queue=None, run=run, lease=None)

    pool = await session.scalar(
        select(RunnerPoolRecord).where(RunnerPoolRecord.id == run.runner_pool_id).with_for_update()
    )
    if pool is None:
        raise CapacityWait("RUNNER_POOL_NOT_FOUND")
    if pool.status == "DRAINING":
        raise CapacityWait("RUNNER_POOL_DRAINING")
    if pool.status != "ACTIVE":
        raise CapacityWait("RUNNER_POOL_DISABLED")

    healthy_after = dispatch_time - timedelta(seconds=heartbeat_ttl_seconds)
    healthy_workers = tuple(
        await session.scalars(
            select(RunnerWorkerRecord).where(
                RunnerWorkerRecord.pool_id == pool.id,
                RunnerWorkerRecord.status == "ACTIVE",
                RunnerWorkerRecord.last_heartbeat_at >= healthy_after,
            )
        )
    )
    if not healthy_workers:
        raise CapacityWait("NO_HEALTHY_RUNNER")
    snapshot = record.payload["run_snapshot"]
    target_type = str(snapshot["target_type"])
    raw_browser = snapshot.get("browser")
    browser = str(raw_browser) if raw_browser is not None else None
    capable_workers = tuple(
        worker
        for worker in healthy_workers
        if _worker_matches(worker, target_type=target_type, browser=browser)
    )
    if not capable_workers:
        raise CapacityWait("RUNNER_CAPABILITY_MISMATCH")
    isolated_workers = tuple(
        worker for worker in capable_workers if _worker_supports_execution_isolation(worker)
    )
    if not isolated_workers:
        raise CapacityWait("RUNNER_ISOLATION_UNAVAILABLE")
    automation_package = snapshot.get("automation_package")
    if not isinstance(automation_package, dict):
        raise ValueError("Outbox Run Snapshot has no automation package runtime")
    runner_type = str(automation_package.get("runner_type", ""))
    image_repository = str(automation_package.get("image_repository", ""))
    package_digest = str(automation_package.get("digest", ""))
    package_workers = tuple(
        worker
        for worker in isolated_workers
        if _worker_hosts_package(
            worker,
            runner_type=runner_type,
            image_repository=image_repository,
            digest=package_digest,
        )
    )
    if not package_workers:
        raise CapacityWait("AUTOMATION_PACKAGE_UNAVAILABLE")
    strongest_isolation = max(_worker_isolation_rank(worker) for worker in package_workers)
    eligible_workers = tuple(
        worker
        for worker in package_workers
        if _worker_isolation_rank(worker) == strongest_isolation
    )

    existing_lease = await session.scalar(
        select(RunnerSlotLeaseRecord).where(
            RunnerSlotLeaseRecord.run_id == run.id,
            RunnerSlotLeaseRecord.status == "ACTIVE",
        )
    )
    if existing_lease is not None:
        return PreparedDispatch(queue=pool.queue_name, run=run, lease=existing_lease)
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
    effective_capacity = min(
        pool.max_concurrency,
        sum(worker.max_slots for worker in eligible_workers),
    )
    if active_leases >= effective_capacity:
        raise CapacityWait("RUNNER_POOL_CAPACITY_EXHAUSTED")
    lease = RunnerSlotLeaseRecord(
        id=uuid4(),
        pool_id=pool.id,
        run_id=run.id,
        status="ACTIVE",
        acquired_at=dispatch_time,
    )
    session.add(lease)
    return PreparedDispatch(queue=pool.queue_name, run=run, lease=lease)


async def dispatch_outbox_batch(
    session_factory: async_sessionmaker[AsyncSession],
    publisher: TaskPublisher,
    *,
    limit: int = 20,
    now: datetime | None = None,
    heartbeat_ttl_seconds: int = 45,
    capacity_poll_seconds: float = 2.0,
) -> DispatchSummary:
    if not 1 <= limit <= 500:
        raise ValueError("Outbox dispatch limit must be between 1 and 500")
    if not 15 <= heartbeat_ttl_seconds <= 600:
        raise ValueError("Runner heartbeat TTL must be between 15 and 600 seconds")
    if not 0.1 <= capacity_poll_seconds <= 60:
        raise ValueError("capacity poll interval must be between 0.1 and 60 seconds")
    dispatch_time = now or datetime.now(UTC)
    published = 0
    failed = 0
    waiting = 0
    async with session_factory() as session:
        async with session.begin():
            records = tuple(
                await session.scalars(
                    select(DispatchOutboxRecord)
                    .where(
                        DispatchOutboxRecord.status == "PENDING",
                        DispatchOutboxRecord.available_at <= dispatch_time,
                    )
                    .order_by(DispatchOutboxRecord.created_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for record in records:
                try:
                    prepared = await _prepare_run_dispatch(
                        session,
                        record,
                        dispatch_time=dispatch_time,
                        heartbeat_ttl_seconds=heartbeat_ttl_seconds,
                    )
                except CapacityWait as exc:
                    waiting += 1
                    run = await session.get(TestRunRecord, record.aggregate_id)
                    if run is not None:
                        run.dispatch_state = "WAITING"
                        run.dispatch_wait_reason = exc.reason
                    record.last_error = exc.reason
                    record.available_at = dispatch_time + timedelta(seconds=capacity_poll_seconds)
                    continue
                if prepared.skip_publish:
                    published += 1
                    record.status = "PUBLISHED"
                    record.processed_at = dispatch_time
                    record.last_error = None
                    continue
                record.attempts += 1
                try:
                    task_name, args, task_id = _task_for(record)
                    options: dict[str, object] = {"args": args, "task_id": task_id}
                    if prepared.queue is not None:
                        options["queue"] = prepared.queue
                    await asyncio.to_thread(publisher.send_task, task_name, **options)
                except Exception as exc:
                    failed += 1
                    record.last_error = str(exc)[:2000]
                    backoff_seconds = min(300, 2 ** min(record.attempts, 8))
                    record.available_at = dispatch_time + timedelta(seconds=backoff_seconds)
                    if prepared.run is not None:
                        prepared.run.dispatch_state = "WAITING"
                        prepared.run.dispatch_wait_reason = "BROKER_PUBLISH_FAILED"
                    if prepared.lease is not None:
                        prepared.lease.status = "RELEASED"
                        prepared.lease.released_at = dispatch_time
                else:
                    published += 1
                    record.status = "PUBLISHED"
                    record.processed_at = dispatch_time
                    record.last_error = None
                    if prepared.run is not None:
                        prepared.run.dispatch_state = "DISPATCHED"
                        prepared.run.dispatch_wait_reason = None
                        prepared.run.dispatched_at = dispatch_time
    return DispatchSummary(
        selected=published + failed + waiting,
        published=published,
        failed=failed,
        waiting=waiting,
    )
