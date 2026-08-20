"""Scheduled quality signal state evaluation and transactional Webhook enqueueing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .persistence import (
    AuditLogRecord,
    ProjectRecord,
    QualityAlertStateRecord,
    QualityWebhookConfigRecord,
    QualityWebhookDeliveryRecord,
    utc_now,
)
from .quality_services import QualityComparisonSnapshot, calculate_quality_comparison
from .quality_webhook_services import endpoint_display
from .schemas import QualityChangeSignal

SYSTEM_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
AlertStatus = Literal["NO_DATA", "STABLE", "WARNING", "CRITICAL"]
ActiveAlertStatus = Literal["WARNING", "CRITICAL"]
QUALITY_ALERT_RANK = {"NO_DATA": 0, "STABLE": 1, "WARNING": 2, "CRITICAL": 3}


@dataclass(frozen=True, slots=True)
class QualityAlertDecision:
    event_type: str | None
    active_notification_status: ActiveAlertStatus | None
    reason: str


@dataclass(frozen=True, slots=True)
class QualityAlertEvaluationSummary:
    selected: int
    evaluated: int
    transitions: int
    queued: int
    cooldown_suppressed: int
    silence_suppressed: int


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def decide_quality_alert_event(
    *,
    current_status: AlertStatus,
    active_notification_status: ActiveAlertStatus | None,
    minimum_alert_status: ActiveAlertStatus,
    cooldown_until: datetime | None,
    now: datetime,
) -> QualityAlertDecision:
    """Resolve one signal without relying on a particular Run terminal path."""

    if current_status == "NO_DATA":
        return QualityAlertDecision(None, active_notification_status, "insufficient_data")
    if active_notification_status is not None:
        if current_status == "STABLE":
            return QualityAlertDecision("quality.alert.recovered", None, "signal_recovered")
        current_rank = QUALITY_ALERT_RANK[current_status]
        active_rank = QUALITY_ALERT_RANK[active_notification_status]
        if current_rank > active_rank:
            return QualityAlertDecision(
                "quality.alert.escalated",
                current_status,
                "severity_increased",
            )
        if current_rank < active_rank:
            next_active = (
                current_status if current_rank >= QUALITY_ALERT_RANK[minimum_alert_status] else None
            )
            return QualityAlertDecision(
                "quality.alert.deescalated",
                next_active,
                "severity_decreased",
            )
        return QualityAlertDecision(None, active_notification_status, "active_state_unchanged")

    if QUALITY_ALERT_RANK[current_status] < QUALITY_ALERT_RANK[minimum_alert_status]:
        return QualityAlertDecision(None, None, "below_minimum_severity")
    if cooldown_until is not None and _aware(cooldown_until) > _aware(now):
        return QualityAlertDecision(None, None, "cooldown_active")
    return QualityAlertDecision(
        "quality.alert.triggered",
        current_status,
        "qualifying_signal",
    )


def _signal_fingerprint(
    project_id: UUID,
    signal: QualityChangeSignal,
    snapshot: QualityComparisonSnapshot,
) -> str:
    document = {
        "project_id": str(project_id),
        "metric": signal.metric,
        "status": signal.alert_status,
        "current_percent": signal.current_percent,
        "previous_percent": signal.previous_percent,
        "delta_percentage_points": signal.delta_percentage_points,
        "latest_completed_at": (
            snapshot.latest_completed_at.isoformat()
            if snapshot.latest_completed_at is not None
            else None
        ),
        "current_terminal_runs": snapshot.runs.total_terminal_runs,
        "previous_terminal_runs": snapshot.previous_runs.total_terminal_runs,
        "current_terminal_cases": snapshot.cases.total_terminal_cases,
        "previous_terminal_cases": snapshot.previous_cases.total_terminal_cases,
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _delivery_payload(
    *,
    delivery_id: UUID,
    event_type: str,
    project: ProjectRecord,
    signal: QualityChangeSignal,
    previous_status: str,
    decision: QualityAlertDecision,
    snapshot: QualityComparisonSnapshot,
    occurred_at: datetime,
) -> dict[str, object]:
    comparison = snapshot.comparison
    return {
        "schema_version": "1.0",
        "event_id": str(delivery_id),
        "event_type": event_type,
        "occurred_at": occurred_at.isoformat(),
        "project": {
            "id": str(project.id),
            "key": project.key,
            "name": project.name,
        },
        "alert": {
            "metric": signal.metric,
            "status": signal.alert_status,
            "previous_status": previous_status,
            "current_percent": signal.current_percent,
            "previous_percent": signal.previous_percent,
            "delta_percentage_points": signal.delta_percentage_points,
            "reason": decision.reason,
        },
        "window": {
            "current_started_at": snapshot.window_started_at.isoformat(),
            "current_ended_at": snapshot.window_ended_at.isoformat(),
            "previous_started_at": comparison.previous_window_started_at.isoformat(),
            "previous_ended_at": comparison.previous_window_ended_at.isoformat(),
            "warning_drop_percentage_points": comparison.warning_drop_percentage_points,
            "critical_drop_percentage_points": comparison.critical_drop_percentage_points,
            "latest_completed_at": (
                snapshot.latest_completed_at.isoformat()
                if snapshot.latest_completed_at is not None
                else None
            ),
        },
    }


async def _evaluate_project(
    session: AsyncSession,
    config: QualityWebhookConfigRecord,
    project: ProjectRecord,
    *,
    moment: datetime,
) -> tuple[int, int, int, int]:
    snapshot = await calculate_quality_comparison(
        session,
        project,
        window_days=project.quality_slo_window_days,
        now=moment,
    )
    existing_states = {
        state.metric: state
        for state in await session.scalars(
            select(QualityAlertStateRecord)
            .where(QualityAlertStateRecord.project_id == project.id)
            .with_for_update()
        )
    }
    transitions = 0
    queued = 0
    cooldown_suppressed = 0
    silence_suppressed = 0
    silence_active = config.silenced_until is not None and _aware(config.silenced_until) > _aware(
        moment
    )

    for signal in snapshot.comparison.signals:
        fingerprint = _signal_fingerprint(project.id, signal, snapshot)
        state = existing_states.get(signal.metric)
        previous_status = state.current_status if state is not None else "NO_DATA"
        status_changed = previous_status != signal.alert_status
        if state is None:
            state = QualityAlertStateRecord(
                id=uuid4(),
                project_id=project.id,
                metric=signal.metric,
                current_status=signal.alert_status,
                active_notification_status=None,
                current_percent=signal.current_percent,
                previous_percent=signal.previous_percent,
                delta_percentage_points=signal.delta_percentage_points,
                signal_fingerprint=fingerprint,
                notification_sequence=0,
                last_evaluated_at=moment,
                last_transition_at=moment,
            )
            session.add(state)
            existing_states[signal.metric] = state
        else:
            state.current_status = signal.alert_status
            state.current_percent = signal.current_percent
            state.previous_percent = signal.previous_percent
            state.delta_percentage_points = signal.delta_percentage_points
            state.signal_fingerprint = fingerprint
            state.last_evaluated_at = moment
            if status_changed:
                state.last_transition_at = moment
                state.acknowledged_at = None
                state.acknowledged_by = None
                state.acknowledgement_note = None

        transitions += int(status_changed)
        decision = decide_quality_alert_event(
            current_status=signal.alert_status,
            active_notification_status=state.active_notification_status,
            minimum_alert_status=config.minimum_alert_status,
            cooldown_until=state.cooldown_until,
            now=moment,
        )
        if decision.reason == "cooldown_active":
            cooldown_suppressed += 1
        event_suppressed_by_silence = decision.event_type is not None and silence_active
        if event_suppressed_by_silence:
            silence_suppressed += 1
        if decision.event_type is None or event_suppressed_by_silence:
            if status_changed:
                session.add(
                    AuditLogRecord(
                        actor_id=SYSTEM_ACTOR_ID,
                        action="project.quality_alert_state_changed",
                        resource_type="quality_alert_state",
                        resource_id=str(state.id),
                        project_id=project.id,
                        details={
                            "metric": signal.metric,
                            "previous_status": previous_status,
                            "status": signal.alert_status,
                            "reason": (
                                "silence_active" if event_suppressed_by_silence else decision.reason
                            ),
                            "signal_fingerprint": fingerprint,
                        },
                        created_at=moment,
                    )
                )
            continue

        state.acknowledged_at = None
        state.acknowledged_by = None
        state.acknowledgement_note = None
        state.notification_sequence += 1
        delivery_id = uuid4()
        delivery = QualityWebhookDeliveryRecord(
            id=delivery_id,
            project_id=project.id,
            webhook_config_id=config.id,
            event_type=decision.event_type,
            dedupe_key=(
                f"quality.alert:{project.id}:{signal.metric}:"
                f"{state.notification_sequence}:{fingerprint}"
            ),
            destination_display=endpoint_display(config.endpoint_url),
            payload=_delivery_payload(
                delivery_id=delivery_id,
                event_type=decision.event_type,
                project=project,
                signal=signal,
                previous_status=previous_status,
                decision=decision,
                snapshot=snapshot,
                occurred_at=moment,
            ),
            status="PENDING",
            attempts=0,
            available_at=moment,
            created_at=moment,
        )
        session.add(delivery)
        state.active_notification_status = decision.active_notification_status
        state.last_notified_at = moment
        state.last_delivery_id = delivery_id
        if decision.active_notification_status is not None:
            state.cooldown_until = moment + timedelta(seconds=config.cooldown_seconds)
        session.add(
            AuditLogRecord(
                actor_id=SYSTEM_ACTOR_ID,
                action="project.quality_alert_queued",
                resource_type="quality_webhook_delivery",
                resource_id=str(delivery_id),
                project_id=project.id,
                details={
                    "event_type": decision.event_type,
                    "metric": signal.metric,
                    "previous_status": previous_status,
                    "status": signal.alert_status,
                    "reason": decision.reason,
                    "destination_display": delivery.destination_display,
                    "signal_fingerprint": fingerprint,
                },
                created_at=moment,
            )
        )
        queued += 1

    return transitions, queued, cooldown_suppressed, silence_suppressed


async def evaluate_quality_alert_batch(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    batch_size: int = 50,
    evaluation_interval_seconds: int = 60,
) -> QualityAlertEvaluationSummary:
    if not 1 <= batch_size <= 500:
        raise ValueError("quality alert batch size must be between 1 and 500")
    if not 5 <= evaluation_interval_seconds <= 3600:
        raise ValueError("quality alert evaluation interval must be between 5 and 3600 seconds")
    moment = _aware(now or utc_now()).astimezone(UTC)
    next_evaluation_at = moment + timedelta(seconds=evaluation_interval_seconds)
    transitions = 0
    queued = 0
    cooldown_suppressed = 0
    silence_suppressed = 0

    async with session_factory() as session:
        async with session.begin():
            configs = tuple(
                await session.scalars(
                    select(QualityWebhookConfigRecord)
                    .where(
                        QualityWebhookConfigRecord.enabled.is_(True),
                        or_(
                            QualityWebhookConfigRecord.next_evaluation_at.is_(None),
                            QualityWebhookConfigRecord.next_evaluation_at <= moment,
                        ),
                    )
                    .order_by(
                        func.coalesce(
                            QualityWebhookConfigRecord.next_evaluation_at,
                            QualityWebhookConfigRecord.created_at,
                        ),
                        QualityWebhookConfigRecord.project_id,
                    )
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            evaluated = 0
            for config in configs:
                project = await session.get(ProjectRecord, config.project_id)
                if project is None:
                    continue
                (
                    project_transitions,
                    project_queued,
                    project_cooldown_suppressed,
                    project_silence_suppressed,
                ) = await _evaluate_project(session, config, project, moment=moment)
                transitions += project_transitions
                queued += project_queued
                cooldown_suppressed += project_cooldown_suppressed
                silence_suppressed += project_silence_suppressed
                evaluated += 1
                config_next_evaluation_at = next_evaluation_at
                if config.silenced_until is not None:
                    silenced_until = _aware(config.silenced_until).astimezone(UTC)
                    if moment < silenced_until < config_next_evaluation_at:
                        config_next_evaluation_at = silenced_until
                await session.execute(
                    update(QualityWebhookConfigRecord)
                    .where(QualityWebhookConfigRecord.id == config.id)
                    .values(
                        last_evaluated_at=moment,
                        next_evaluation_at=config_next_evaluation_at,
                        updated_at=QualityWebhookConfigRecord.updated_at,
                    )
                )

    return QualityAlertEvaluationSummary(
        selected=len(configs),
        evaluated=evaluated,
        transitions=transitions,
        queued=queued,
        cooldown_suppressed=cooldown_suppressed,
        silence_suppressed=silence_suppressed,
    )
