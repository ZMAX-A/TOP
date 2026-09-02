"""Authorized short-lived tickets for active, read-only Run live views."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from redis.asyncio import Redis

from testops.contracts import (
    LiveViewRegistration,
    LiveViewTicketRecord,
    RunSnapshot,
    live_view_session_key,
    live_view_ticket_key,
)

from .persistence import TestRunRecord
from .schemas import RunLiveViewStatusResponse, RunLiveViewTicketResponse
from .services import ResourceConflict, ServiceError

TERMINAL_RUN_STATUSES = frozenset(
    {"PASSED", "FAILED", "CANCELED", "TIMED_OUT", "INFRA_ERROR"}
)


class LiveViewBackendUnavailable(ServiceError):
    status_code = 503


class LiveViewStore(Protocol):
    def get_registration(self, run_id: UUID) -> Awaitable[LiveViewRegistration | None]: ...

    def issue_ticket(
        self,
        registration: LiveViewRegistration,
        *,
        ttl_seconds: int,
    ) -> Awaitable[tuple[str, datetime]]: ...

    def aclose(self) -> Awaitable[None]: ...


class RedisLiveViewStore:
    def __init__(self, redis_url: str, *, client: Any | None = None):
        self._client = client or Redis.from_url(redis_url, decode_responses=True)

    async def get_registration(self, run_id: UUID) -> LiveViewRegistration | None:
        try:
            document = await self._client.get(live_view_session_key(run_id))
        except Exception as exc:
            raise LiveViewBackendUnavailable("live-view registry is unavailable") from exc
        if not document:
            return None
        try:
            registration = LiveViewRegistration.model_validate_json(document)
        except ValueError:
            return None
        return registration if registration.run_id == run_id else None

    async def issue_ticket(
        self,
        registration: LiveViewRegistration,
        *,
        ttl_seconds: int,
    ) -> tuple[str, datetime]:
        record = LiveViewTicketRecord(
            run_id=registration.run_id,
            session_id=registration.session_id,
        )
        for _attempt in range(3):
            ticket = secrets.token_urlsafe(32)
            try:
                stored = await self._client.set(
                    live_view_ticket_key(ticket),
                    record.model_dump_json(),
                    ex=ttl_seconds,
                    nx=True,
                )
            except Exception as exc:
                raise LiveViewBackendUnavailable("live-view ticket service is unavailable") from exc
            if stored:
                return ticket, datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        raise LiveViewBackendUnavailable("live-view ticket could not be issued")

    async def aclose(self) -> None:
        await self._client.aclose()


def _headed_snapshot(run: TestRunRecord) -> bool:
    snapshot = RunSnapshot.model_validate(run.snapshot)
    return snapshot.web_config is not None and not snapshot.web_config.headless


async def live_view_status(
    run: TestRunRecord,
    store: LiveViewStore,
) -> RunLiveViewStatusResponse:
    if not _headed_snapshot(run):
        return RunLiveViewStatusResponse(status="DISABLED", read_only=True)
    if run.status in TERMINAL_RUN_STATUSES:
        return RunLiveViewStatusResponse(status="ENDED", read_only=True)
    try:
        registration = await store.get_registration(run.id)
    except LiveViewBackendUnavailable:
        return RunLiveViewStatusResponse(status="UNAVAILABLE", read_only=True)
    return RunLiveViewStatusResponse(
        status="READY" if registration is not None else "STARTING",
        read_only=True,
    )


async def issue_live_view_ticket(
    run: TestRunRecord,
    store: LiveViewStore,
    *,
    ttl_seconds: int,
) -> RunLiveViewTicketResponse:
    if not _headed_snapshot(run):
        raise ResourceConflict("live view is only available for headed Runs")
    if run.status in TERMINAL_RUN_STATUSES:
        raise ResourceConflict("live view ended with the Run")
    registration = await store.get_registration(run.id)
    if registration is None:
        raise ResourceConflict("live view is not ready")
    ticket, expires_at = await store.issue_ticket(registration, ttl_seconds=ttl_seconds)
    return RunLiveViewTicketResponse(
        ticket=ticket,
        password=registration.password,
        viewer_path="/viewer/session.html",
        read_only=True,
        expires_at=expires_at,
    )
