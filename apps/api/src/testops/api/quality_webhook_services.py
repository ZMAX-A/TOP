"""Project quality Webhook configuration and durable delivery services."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .persistence import (
    AuditLogRecord,
    ProjectRecord,
    QualityAlertStateRecord,
    QualityWebhookConfigRecord,
    QualityWebhookDeliveryRecord,
    UserRecord,
    utc_now,
)
from .schemas import (
    QualityAlertAcknowledgementUpdate,
    QualityAlertMetric,
    QualityAlertSilenceUpdate,
    QualityAlertStateResponse,
    QualityWebhookConfigResponse,
    QualityWebhookConfigUpdate,
    QualityWebhookDeliveryResponse,
    QualityWebhookReplayRequest,
)
from .services import InvalidRequest, ResourceConflict, ResourceNotFound

MAX_QUALITY_ALERT_SILENCE = timedelta(days=30)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def endpoint_display(endpoint_url: str) -> str:
    """Return a stable destination label without exposing path or query credentials."""

    parsed = urlsplit(endpoint_url)
    host = parsed.hostname or "unknown"
    display_host = f"[{host}]" if ":" in host else host
    authority = display_host if parsed.port in {None, 443} else f"{display_host}:{parsed.port}"
    return f"https://{authority}/***"


async def _project(
    session: AsyncSession,
    project_id: UUID,
    *,
    for_update: bool = False,
) -> ProjectRecord:
    if for_update:
        project = await session.scalar(
            select(ProjectRecord).where(ProjectRecord.id == project_id).with_for_update()
        )
    else:
        project = await session.get(ProjectRecord, project_id)
    if project is None:
        raise ResourceNotFound("project not found")
    return project


async def _config(
    session: AsyncSession,
    project_id: UUID,
    *,
    for_update: bool = False,
) -> QualityWebhookConfigRecord | None:
    statement = select(QualityWebhookConfigRecord).where(
        QualityWebhookConfigRecord.project_id == project_id
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


def _config_response(
    project_id: UUID,
    config: QualityWebhookConfigRecord | None,
    operator_names: Mapping[UUID, str] | None = None,
) -> QualityWebhookConfigResponse:
    names = operator_names or {}
    if config is None:
        return QualityWebhookConfigResponse(
            project_id=project_id,
            enabled=False,
            endpoint_configured=False,
            endpoint_display=None,
            minimum_alert_status="WARNING",
            cooldown_seconds=3600,
            signing_configured=False,
            last_evaluated_at=None,
            next_evaluation_at=None,
            silenced_until=None,
            silenced_by=None,
            silenced_by_display_name=None,
            silence_reason=None,
            updated_at=None,
        )
    return QualityWebhookConfigResponse(
        project_id=project_id,
        enabled=config.enabled,
        endpoint_configured=True,
        endpoint_display=endpoint_display(config.endpoint_url),
        minimum_alert_status=config.minimum_alert_status,
        cooldown_seconds=config.cooldown_seconds,
        signing_configured=config.signing_secret_name is not None,
        last_evaluated_at=config.last_evaluated_at,
        next_evaluation_at=config.next_evaluation_at,
        silenced_until=config.silenced_until,
        silenced_by=config.silenced_by,
        silenced_by_display_name=(
            names.get(config.silenced_by) if config.silenced_by is not None else None
        ),
        silence_reason=config.silence_reason,
        updated_at=config.updated_at,
    )


def _delivery_response(
    delivery: QualityWebhookDeliveryRecord,
    operator_names: Mapping[UUID, str] | None = None,
) -> QualityWebhookDeliveryResponse:
    names = operator_names or {}
    return QualityWebhookDeliveryResponse(
        id=delivery.id,
        project_id=delivery.project_id,
        event_type=delivery.event_type,
        destination_display=delivery.destination_display,
        status=delivery.status,
        attempts=delivery.attempts,
        response_status=delivery.response_status,
        last_error=delivery.last_error,
        replay_of_id=delivery.replay_of_id,
        replayed_by=delivery.replayed_by,
        replayed_by_display_name=(
            names.get(delivery.replayed_by) if delivery.replayed_by is not None else None
        ),
        replay_reason=delivery.replay_reason,
        created_at=delivery.created_at,
        delivered_at=delivery.delivered_at,
    )


def _state_response(
    state: QualityAlertStateRecord,
    operator_names: Mapping[UUID, str] | None = None,
) -> QualityAlertStateResponse:
    names = operator_names or {}
    return QualityAlertStateResponse(
        project_id=state.project_id,
        metric=state.metric,
        current_status=state.current_status,
        active_notification_status=state.active_notification_status,
        current_percent=state.current_percent,
        previous_percent=state.previous_percent,
        delta_percentage_points=state.delta_percentage_points,
        notification_sequence=state.notification_sequence,
        last_evaluated_at=state.last_evaluated_at,
        last_transition_at=state.last_transition_at,
        last_notified_at=state.last_notified_at,
        cooldown_until=state.cooldown_until,
        last_delivery_id=state.last_delivery_id,
        acknowledged_at=state.acknowledged_at,
        acknowledged_by=state.acknowledged_by,
        acknowledged_by_display_name=(
            names.get(state.acknowledged_by) if state.acknowledged_by is not None else None
        ),
        acknowledgement_note=state.acknowledgement_note,
    )


async def _operator_names(
    session: AsyncSession,
    actor_ids: Iterable[UUID | None],
) -> dict[UUID, str]:
    resolved_ids = {actor_id for actor_id in actor_ids if actor_id is not None}
    if not resolved_ids:
        return {}
    rows = await session.execute(
        select(UserRecord.id, UserRecord.display_name).where(UserRecord.id.in_(resolved_ids))
    )
    return {user_id: display_name for user_id, display_name in rows}


async def _config_response_with_names(
    session: AsyncSession,
    project_id: UUID,
    config: QualityWebhookConfigRecord | None,
) -> QualityWebhookConfigResponse:
    names = await _operator_names(
        session,
        (config.silenced_by if config is not None else None,),
    )
    return _config_response(project_id, config, names)


async def _delivery_response_with_names(
    session: AsyncSession,
    delivery: QualityWebhookDeliveryRecord,
) -> QualityWebhookDeliveryResponse:
    names = await _operator_names(session, (delivery.replayed_by,))
    return _delivery_response(delivery, names)


async def _state_response_with_names(
    session: AsyncSession,
    state: QualityAlertStateRecord,
) -> QualityAlertStateResponse:
    names = await _operator_names(session, (state.acknowledged_by,))
    return _state_response(state, names)


def _audit_config(config: QualityWebhookConfigRecord | None) -> dict[str, object]:
    if config is None:
        return {
            "enabled": False,
            "endpoint_configured": False,
            "minimum_alert_status": "WARNING",
            "signing_configured": False,
            "silenced_until": None,
        }
    return {
        "enabled": config.enabled,
        "endpoint_configured": True,
        "endpoint_display": endpoint_display(config.endpoint_url),
        "minimum_alert_status": config.minimum_alert_status,
        "cooldown_seconds": config.cooldown_seconds,
        "signing_configured": config.signing_secret_name is not None,
        "silenced_until": (
            config.silenced_until.isoformat() if config.silenced_until is not None else None
        ),
    }


async def get_quality_webhook_config(
    session: AsyncSession,
    project_id: UUID,
) -> QualityWebhookConfigResponse:
    await _project(session, project_id)
    return await _config_response_with_names(
        session,
        project_id,
        await _config(session, project_id),
    )


async def update_quality_webhook_config(
    session: AsyncSession,
    project_id: UUID,
    payload: QualityWebhookConfigUpdate,
    actor_id: UUID,
) -> QualityWebhookConfigResponse:
    await _project(session, project_id, for_update=True)
    config = await _config(session, project_id, for_update=True)
    before = _audit_config(config)
    was_enabled = config.enabled if config is not None else False
    was_minimum_alert_status = config.minimum_alert_status if config is not None else "WARNING"
    if config is None:
        if payload.endpoint_url is None:
            raise InvalidRequest("endpoint_url is required when configuring a quality webhook")
        config = QualityWebhookConfigRecord(
            id=uuid4(),
            project_id=project_id,
            enabled=False,
            endpoint_url=payload.endpoint_url,
            minimum_alert_status="WARNING",
            cooldown_seconds=3600,
        )
        session.add(config)

    if payload.endpoint_url is not None:
        config.endpoint_url = payload.endpoint_url
    if payload.enabled is not None:
        config.enabled = payload.enabled
    if payload.minimum_alert_status is not None:
        config.minimum_alert_status = payload.minimum_alert_status
    if payload.cooldown_seconds is not None:
        config.cooldown_seconds = payload.cooldown_seconds
    if payload.clear_signing_secret:
        config.signing_secret_name = None
        config.signing_secret_ref = None
    elif {"signing_secret_name", "signing_secret_ref"}.issubset(payload.model_fields_set):
        config.signing_secret_name = payload.signing_secret_name
        config.signing_secret_ref = payload.signing_secret_ref

    activation_changed = (
        was_enabled != config.enabled or was_minimum_alert_status != config.minimum_alert_status
    )
    if config.enabled:
        if activation_changed or config.next_evaluation_at is None:
            config.next_evaluation_at = utc_now()
    else:
        config.next_evaluation_at = None
    if activation_changed:
        await session.execute(
            update(QualityAlertStateRecord)
            .where(QualityAlertStateRecord.project_id == project_id)
            .values(
                active_notification_status=None,
                cooldown_until=None,
                acknowledged_at=None,
                acknowledged_by=None,
                acknowledgement_note=None,
            )
        )

    session.add(
        AuditLogRecord(
            actor_id=actor_id,
            action="project.quality_webhook_updated",
            resource_type="project_quality_webhook",
            resource_id=str(config.id),
            project_id=project_id,
            details={"before": before, "after": _audit_config(config)},
        )
    )
    await session.commit()
    await session.refresh(config)
    return await _config_response_with_names(session, project_id, config)


async def set_quality_alert_silence(
    session: AsyncSession,
    project_id: UUID,
    payload: QualityAlertSilenceUpdate,
    actor_id: UUID,
    *,
    now: datetime | None = None,
) -> QualityWebhookConfigResponse:
    await _project(session, project_id, for_update=True)
    config = await _config(session, project_id, for_update=True)
    if config is None or not config.enabled:
        raise ResourceConflict("quality webhook must be configured and enabled before silencing")
    moment = _aware(now or utc_now()).astimezone(UTC)
    silenced_until = _aware(payload.silenced_until).astimezone(UTC)
    if silenced_until <= moment:
        raise InvalidRequest("quality alert silence must end in the future")
    if silenced_until > moment + MAX_QUALITY_ALERT_SILENCE:
        raise InvalidRequest("quality alert silence cannot exceed 30 days")
    previous_until = config.silenced_until
    config.silenced_until = silenced_until
    config.silenced_by = actor_id
    config.silence_reason = payload.reason
    config.next_evaluation_at = moment
    session.add(
        AuditLogRecord(
            actor_id=actor_id,
            action="project.quality_alert_silenced",
            resource_type="project_quality_webhook",
            resource_id=str(config.id),
            project_id=project_id,
            details={
                "previous_silenced_until": (
                    previous_until.isoformat() if previous_until is not None else None
                ),
                "silenced_until": silenced_until.isoformat(),
                "reason": payload.reason,
            },
            created_at=moment,
        )
    )
    await session.commit()
    await session.refresh(config)
    return await _config_response_with_names(session, project_id, config)


async def clear_quality_alert_silence(
    session: AsyncSession,
    project_id: UUID,
    actor_id: UUID,
    *,
    now: datetime | None = None,
) -> QualityWebhookConfigResponse:
    await _project(session, project_id, for_update=True)
    config = await _config(session, project_id, for_update=True)
    if config is None:
        raise ResourceConflict("quality webhook is not configured")
    if config.silenced_until is None:
        return await _config_response_with_names(session, project_id, config)
    moment = _aware(now or utc_now()).astimezone(UTC)
    previous_until = config.silenced_until
    config.silenced_until = None
    config.silenced_by = None
    config.silence_reason = None
    config.next_evaluation_at = moment if config.enabled else None
    session.add(
        AuditLogRecord(
            actor_id=actor_id,
            action="project.quality_alert_silence_cleared",
            resource_type="project_quality_webhook",
            resource_id=str(config.id),
            project_id=project_id,
            details={"previous_silenced_until": previous_until.isoformat()},
            created_at=moment,
        )
    )
    await session.commit()
    await session.refresh(config)
    return await _config_response_with_names(session, project_id, config)


async def enqueue_quality_webhook_test(
    session: AsyncSession,
    project_id: UUID,
    actor_id: UUID,
    *,
    now: datetime | None = None,
) -> QualityWebhookDeliveryResponse:
    project = await _project(session, project_id, for_update=True)
    config = await _config(session, project_id, for_update=True)
    if config is None or not config.enabled:
        raise ResourceConflict("quality webhook must be configured and enabled before testing")
    moment = now or utc_now()
    delivery_id = uuid4()
    delivery = QualityWebhookDeliveryRecord(
        id=delivery_id,
        project_id=project_id,
        webhook_config_id=config.id,
        event_type="quality.alert.test",
        dedupe_key=f"quality.alert.test:{delivery_id}",
        destination_display=endpoint_display(config.endpoint_url),
        payload={
            "schema_version": "1.0",
            "event_id": str(delivery_id),
            "event_type": "quality.alert.test",
            "occurred_at": moment.isoformat(),
            "project": {
                "id": str(project.id),
                "key": project.key,
                "name": project.name,
            },
            "alert": {
                "status": "TEST",
                "message": "TestOps Platform quality Webhook test delivery",
            },
        },
        status="PENDING",
        attempts=0,
        available_at=moment,
        created_at=moment,
    )
    session.add(delivery)
    session.add(
        AuditLogRecord(
            actor_id=actor_id,
            action="project.quality_webhook_test_queued",
            resource_type="quality_webhook_delivery",
            resource_id=str(delivery.id),
            project_id=project_id,
            details={
                "event_type": delivery.event_type,
                "destination_display": delivery.destination_display,
            },
        )
    )
    await session.commit()
    await session.refresh(delivery)
    return await _delivery_response_with_names(session, delivery)


async def list_quality_webhook_deliveries(
    session: AsyncSession,
    project_id: UUID,
    *,
    limit: int = 20,
) -> tuple[QualityWebhookDeliveryResponse, ...]:
    await _project(session, project_id)
    deliveries = tuple(
        await session.scalars(
            select(QualityWebhookDeliveryRecord)
            .where(QualityWebhookDeliveryRecord.project_id == project_id)
            .order_by(QualityWebhookDeliveryRecord.created_at.desc())
            .limit(limit)
        )
    )
    operator_names = await _operator_names(
        session,
        (delivery.replayed_by for delivery in deliveries),
    )
    return tuple(_delivery_response(delivery, operator_names) for delivery in deliveries)


async def replay_failed_quality_webhook_delivery(
    session: AsyncSession,
    project_id: UUID,
    delivery_id: UUID,
    payload: QualityWebhookReplayRequest,
    actor_id: UUID,
    *,
    now: datetime | None = None,
) -> QualityWebhookDeliveryResponse:
    await _project(session, project_id, for_update=True)
    source = await session.scalar(
        select(QualityWebhookDeliveryRecord)
        .where(
            QualityWebhookDeliveryRecord.id == delivery_id,
            QualityWebhookDeliveryRecord.project_id == project_id,
        )
        .with_for_update()
    )
    if source is None:
        raise ResourceNotFound("quality webhook delivery not found")
    if source.status != "FAILED":
        raise ResourceConflict("only a failed quality webhook delivery can be replayed")
    existing_replay = await session.scalar(
        select(QualityWebhookDeliveryRecord).where(
            QualityWebhookDeliveryRecord.replay_of_id == source.id
        )
    )
    if existing_replay is not None:
        raise ResourceConflict("quality webhook delivery has already been replayed")
    config = await _config(session, project_id, for_update=True)
    if config is None or not config.enabled:
        raise ResourceConflict("quality webhook must be configured and enabled before replaying")

    moment = _aware(now or utc_now()).astimezone(UTC)
    replay = QualityWebhookDeliveryRecord(
        id=uuid4(),
        project_id=project_id,
        webhook_config_id=config.id,
        event_type=source.event_type,
        dedupe_key=f"quality.webhook.replay:{source.id}",
        destination_display=endpoint_display(config.endpoint_url),
        payload=deepcopy(source.payload),
        status="PENDING",
        attempts=0,
        available_at=moment,
        replay_of_id=source.id,
        replayed_by=actor_id,
        replay_reason=payload.reason,
        created_at=moment,
    )
    session.add(replay)
    session.add(
        AuditLogRecord(
            actor_id=actor_id,
            action="project.quality_webhook_delivery_replayed",
            resource_type="quality_webhook_delivery",
            resource_id=str(replay.id),
            project_id=project_id,
            details={
                "source_delivery_id": str(source.id),
                "event_type": source.event_type,
                "source_attempts": source.attempts,
                "source_response_status": source.response_status,
                "destination_display": replay.destination_display,
                "reason": payload.reason,
            },
            created_at=moment,
        )
    )
    await session.commit()
    await session.refresh(replay)
    return await _delivery_response_with_names(session, replay)


async def acknowledge_quality_alert(
    session: AsyncSession,
    project_id: UUID,
    metric: QualityAlertMetric,
    payload: QualityAlertAcknowledgementUpdate,
    actor_id: UUID,
    *,
    now: datetime | None = None,
) -> QualityAlertStateResponse:
    await _project(session, project_id, for_update=True)
    state = await session.scalar(
        select(QualityAlertStateRecord)
        .where(
            QualityAlertStateRecord.project_id == project_id,
            QualityAlertStateRecord.metric == metric,
        )
        .with_for_update()
    )
    if state is None:
        raise ResourceNotFound("quality alert state not found")
    if state.current_status not in {"WARNING", "CRITICAL"}:
        raise ResourceConflict("only an active quality alert can be acknowledged")
    moment = _aware(now or utc_now()).astimezone(UTC)
    state.acknowledged_at = moment
    state.acknowledged_by = actor_id
    state.acknowledgement_note = payload.note
    session.add(
        AuditLogRecord(
            actor_id=actor_id,
            action="project.quality_alert_acknowledged",
            resource_type="quality_alert_state",
            resource_id=str(state.id),
            project_id=project_id,
            details={
                "metric": state.metric,
                "status": state.current_status,
                "note": payload.note,
                "signal_fingerprint": state.signal_fingerprint,
            },
            created_at=moment,
        )
    )
    await session.commit()
    await session.refresh(state)
    return await _state_response_with_names(session, state)


async def clear_quality_alert_acknowledgement(
    session: AsyncSession,
    project_id: UUID,
    metric: QualityAlertMetric,
    actor_id: UUID,
    *,
    now: datetime | None = None,
) -> QualityAlertStateResponse:
    await _project(session, project_id, for_update=True)
    state = await session.scalar(
        select(QualityAlertStateRecord)
        .where(
            QualityAlertStateRecord.project_id == project_id,
            QualityAlertStateRecord.metric == metric,
        )
        .with_for_update()
    )
    if state is None:
        raise ResourceNotFound("quality alert state not found")
    if state.acknowledged_at is None:
        return await _state_response_with_names(session, state)
    moment = _aware(now or utc_now()).astimezone(UTC)
    previous_acknowledged_at = state.acknowledged_at
    state.acknowledged_at = None
    state.acknowledged_by = None
    state.acknowledgement_note = None
    session.add(
        AuditLogRecord(
            actor_id=actor_id,
            action="project.quality_alert_acknowledgement_cleared",
            resource_type="quality_alert_state",
            resource_id=str(state.id),
            project_id=project_id,
            details={
                "metric": state.metric,
                "status": state.current_status,
                "previous_acknowledged_at": previous_acknowledged_at.isoformat(),
            },
            created_at=moment,
        )
    )
    await session.commit()
    await session.refresh(state)
    return await _state_response_with_names(session, state)


async def list_quality_alert_states(
    session: AsyncSession,
    project_id: UUID,
) -> tuple[QualityAlertStateResponse, ...]:
    await _project(session, project_id)
    states = tuple(
        await session.scalars(
            select(QualityAlertStateRecord)
            .where(QualityAlertStateRecord.project_id == project_id)
            .order_by(QualityAlertStateRecord.metric)
        )
    )
    operator_names = await _operator_names(
        session,
        (state.acknowledged_by for state in states),
    )
    return tuple(_state_response(state, operator_names) for state in states)
