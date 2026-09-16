"""User-managed Run result email preferences and durable enqueueing."""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .identity_schemas import (
    RunEmailDeliveryResponse,
    RunEmailPreferenceResponse,
    RunEmailPreferenceUpdate,
    RunEmailStatus,
)
from .persistence import (
    AuditLogRecord,
    ProjectRecord,
    RunEmailDeliveryRecord,
    RunEmailPreferenceRecord,
    TestRunRecord,
    UserRecord,
    utc_now,
)
from .services import ResourceConflict, ResourceNotFound

DEFAULT_STATUSES = tuple(RunEmailStatus)


def masked_email(email: str) -> str:
    local, _, domain = email.rpartition("@")
    visible = local[:1] if local else "*"
    return f"{visible}***@{domain}"


def _preference_emails(preference: RunEmailPreferenceRecord | None) -> tuple[str, ...]:
    if preference is None:
        return ()
    if preference.emails:
        return tuple(str(item) for item in preference.emails)
    return (preference.email,) if preference.email is not None else ()


def _selected_preference_emails(
    preference: RunEmailPreferenceRecord | None,
) -> tuple[str, ...]:
    if preference is None:
        return ()
    configured = _preference_emails(preference)
    selected = preference.selected_emails
    if selected is None:
        return configured
    configured_keys = {item.casefold() for item in configured}
    return tuple(
        str(item)
        for item in selected
        if str(item).casefold() in configured_keys
    )


def _delivery_response(delivery: RunEmailDeliveryRecord) -> RunEmailDeliveryResponse:
    return RunEmailDeliveryResponse(
        id=delivery.id,
        event_type=delivery.event_type,
        run_id=delivery.run_id,
        destination_display=delivery.destination_display,
        status=delivery.status,
        attempts=delivery.attempts,
        last_error=delivery.last_error,
        created_at=delivery.created_at,
        delivered_at=delivery.delivered_at,
    )


async def _last_delivery(
    session: AsyncSession,
    user_id: UUID,
) -> RunEmailDeliveryRecord | None:
    return await session.scalar(
        select(RunEmailDeliveryRecord)
        .where(RunEmailDeliveryRecord.user_id == user_id)
        .order_by(RunEmailDeliveryRecord.created_at.desc())
        .limit(1)
    )


async def get_run_email_preference(
    session: AsyncSession,
    user_id: UUID,
    *,
    smtp_configured: bool,
) -> RunEmailPreferenceResponse:
    preference = await session.get(RunEmailPreferenceRecord, user_id)
    latest = await _last_delivery(session, user_id)
    recipients = _preference_emails(preference)
    selected_recipients = _selected_preference_emails(preference)
    return RunEmailPreferenceResponse(
        email=recipients[0] if recipients else None,
        emails=recipients,
        selected_emails=selected_recipients,
        enabled=preference.enabled if preference is not None else False,
        statuses=(
            tuple(RunEmailStatus(value) for value in preference.statuses)
            if preference is not None
            else DEFAULT_STATUSES
        ),
        smtp_configured=smtp_configured,
        updated_at=preference.updated_at if preference is not None else None,
        last_delivery=_delivery_response(latest) if latest is not None else None,
    )


async def update_run_email_preference(
    session: AsyncSession,
    user_id: UUID,
    payload: RunEmailPreferenceUpdate,
    *,
    smtp_configured: bool,
) -> RunEmailPreferenceResponse:
    if payload.enabled and not smtp_configured:
        raise ResourceConflict("SMTP email delivery is not configured")
    user = await session.get(UserRecord, user_id)
    if user is None:
        raise ResourceNotFound("user not found")
    preference = await session.scalar(
        select(RunEmailPreferenceRecord)
        .where(RunEmailPreferenceRecord.user_id == user_id)
        .with_for_update()
    )
    previous_emails = _preference_emails(preference)
    previous_selected_emails = _selected_preference_emails(preference)
    previous_enabled = preference.enabled if preference is not None else False
    if preference is None:
        preference = RunEmailPreferenceRecord(user_id=user_id)
        session.add(preference)
    recipients = payload.emails or ()
    selected_recipients = payload.selected_emails or ()
    preference.email = recipients[0] if recipients else None
    preference.emails = list(recipients)
    preference.selected_emails = list(selected_recipients)
    preference.enabled = payload.enabled
    preference.statuses = [status.value for status in payload.statuses]

    if (
        not payload.enabled
        or previous_emails != recipients
        or previous_selected_emails != selected_recipients
    ):
        await session.execute(
            update(RunEmailDeliveryRecord)
            .where(
                RunEmailDeliveryRecord.user_id == user_id,
                RunEmailDeliveryRecord.status == "PENDING",
            )
            .values(
                status="CANCELED",
                last_error="Notification preference changed before delivery",
            )
        )
    session.add(
        AuditLogRecord(
            actor_id=user_id,
            action="identity.run_email_preference_updated",
            resource_type="run_email_preference",
            resource_id=str(user_id),
            details={
                "before": {
                    "enabled": previous_enabled,
                    "recipient_count": len(previous_emails),
                    "selected_recipient_count": len(previous_selected_emails),
                },
                "after": {
                    "enabled": payload.enabled,
                    "recipient_count": len(recipients),
                    "selected_recipient_count": len(selected_recipients),
                    "statuses": [status.value for status in payload.statuses],
                },
            },
        )
    )
    await session.commit()
    await session.refresh(preference)
    return await get_run_email_preference(
        session,
        user_id,
        smtp_configured=smtp_configured,
    )


async def enqueue_run_email_test(
    session: AsyncSession,
    user_id: UUID,
    *,
    smtp_configured: bool,
) -> RunEmailDeliveryResponse:
    if not smtp_configured:
        raise ResourceConflict("SMTP email delivery is not configured")
    preference = await session.get(RunEmailPreferenceRecord, user_id)
    recipients = _selected_preference_emails(preference)
    if not recipients:
        raise ResourceConflict(
            "select at least one configured email before sending a test notification"
        )
    moment = utc_now()
    deliveries = tuple(
        RunEmailDeliveryRecord(
            id=uuid4(),
            user_id=user_id,
            event_type="run.email.test",
            dedupe_key=f"run.email.test:{uuid4()}",
            recipient_email=recipient,
            destination_display=masked_email(recipient),
            payload={"kind": "test", "occurred_at": moment.isoformat()},
            status="PENDING",
            attempts=0,
            available_at=moment,
            created_at=moment,
        )
        for recipient in recipients
    )
    session.add_all(deliveries)
    session.add(
        AuditLogRecord(
            actor_id=user_id,
            action="identity.run_email_test_queued",
            resource_type="run_email_delivery",
            resource_id=str(deliveries[0].id),
            details={
                "recipient_count": len(deliveries),
                "destination_displays": [item.destination_display for item in deliveries],
            },
        )
    )
    await session.commit()
    await session.refresh(deliveries[0])
    return _delivery_response(deliveries[0])


def _status_counts(record: TestRunRecord) -> dict[str, int]:
    document = record.result_document or {}
    raw_results = document.get("case_results", [])
    counts = Counter(
        str(item.get("status"))
        for item in raw_results
        if isinstance(item, dict) and item.get("status")
    )
    return dict(sorted(counts.items()))


async def enqueue_run_result_email(
    session: AsyncSession,
    record: TestRunRecord,
) -> tuple[RunEmailDeliveryRecord, ...]:
    preference = await session.get(RunEmailPreferenceRecord, record.created_by)
    recipients = _selected_preference_emails(preference)
    if (
        preference is None
        or not preference.enabled
        or not recipients
        or record.status not in preference.statuses
    ):
        return ()
    project = await session.get(ProjectRecord, record.project_id)
    if project is None:
        return ()
    snapshot = record.snapshot if isinstance(record.snapshot, dict) else {}
    baseline = snapshot.get("case_baseline", {})
    package = snapshot.get("automation_package", {})
    duration_ms = None
    if record.started_at is not None and record.finished_at is not None:
        duration_ms = max(0, int((record.finished_at - record.started_at).total_seconds() * 1000))
    payload = {
        "kind": "run_result",
        "project": {"id": str(project.id), "key": project.key, "name": project.name},
        "run": {
            "id": str(record.id),
            "status": record.status,
            "case_count": record.case_count,
            "status_counts": _status_counts(record),
            "duration_ms": duration_ms,
            "finished_at": record.finished_at.isoformat() if record.finished_at else None,
            "baseline_version": baseline.get("version") if isinstance(baseline, dict) else None,
            "package_version": package.get("version") if isinstance(package, dict) else None,
        },
    }
    moment = utc_now()
    deliveries = tuple(
        RunEmailDeliveryRecord(
            id=uuid4(),
            user_id=record.created_by,
            project_id=record.project_id,
            run_id=record.id,
            event_type="run.result",
            dedupe_key=(
                f"run.result:{record.id}:{record.created_by}:"
                f"{sha256(recipient.casefold().encode('utf-8')).hexdigest()[:16]}"
            ),
            recipient_email=recipient,
            destination_display=masked_email(recipient),
            payload=payload,
            status="PENDING",
            attempts=0,
            available_at=moment,
        )
        for recipient in recipients
    )
    session.add_all(deliveries)
    return deliveries
