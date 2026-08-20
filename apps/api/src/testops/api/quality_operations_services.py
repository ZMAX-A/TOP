"""Low-cardinality quality alert and Webhook operations snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .persistence import QualityWebhookConfigRecord, QualityWebhookDeliveryRecord


@dataclass(frozen=True, slots=True)
class QualityOperationsSnapshot:
    due_evaluations: int
    evaluation_lag_seconds: float
    active_silences: int
    pending_deliveries: int
    failed_deliveries: int
    oldest_pending_age_seconds: float
    replay_deliveries: int


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def collect_quality_operations_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
) -> QualityOperationsSnapshot:
    moment = now or datetime.now(UTC)
    async with session_factory() as session:
        due_evaluations, oldest_evaluation = (
            await session.execute(
                select(
                    func.count(),
                    func.min(QualityWebhookConfigRecord.next_evaluation_at),
                ).where(
                    QualityWebhookConfigRecord.enabled.is_(True),
                    QualityWebhookConfigRecord.next_evaluation_at.is_not(None),
                    QualityWebhookConfigRecord.next_evaluation_at <= moment,
                )
            )
        ).one()
        active_silences = await session.scalar(
            select(func.count())
            .select_from(QualityWebhookConfigRecord)
            .where(
                QualityWebhookConfigRecord.enabled.is_(True),
                QualityWebhookConfigRecord.silenced_until.is_not(None),
                QualityWebhookConfigRecord.silenced_until > moment,
            )
        )
        delivery_rows = await session.execute(
            select(QualityWebhookDeliveryRecord.status, func.count())
            .where(QualityWebhookDeliveryRecord.status.in_(("PENDING", "FAILED")))
            .group_by(QualityWebhookDeliveryRecord.status)
        )
        delivery_counts = {status: int(count) for status, count in delivery_rows}
        oldest_pending = await session.scalar(
            select(func.min(QualityWebhookDeliveryRecord.created_at)).where(
                QualityWebhookDeliveryRecord.status == "PENDING"
            )
        )
        replay_deliveries = await session.scalar(
            select(func.count())
            .select_from(QualityWebhookDeliveryRecord)
            .where(QualityWebhookDeliveryRecord.replay_of_id.is_not(None))
        )

    evaluation_lag = (
        max(0.0, (moment - _aware(oldest_evaluation)).total_seconds())
        if oldest_evaluation is not None
        else 0.0
    )
    pending_age = (
        max(0.0, (moment - _aware(oldest_pending)).total_seconds())
        if oldest_pending is not None
        else 0.0
    )
    return QualityOperationsSnapshot(
        due_evaluations=int(due_evaluations or 0),
        evaluation_lag_seconds=evaluation_lag,
        active_silences=int(active_silences or 0),
        pending_deliveries=delivery_counts.get("PENDING", 0),
        failed_deliveries=delivery_counts.get("FAILED", 0),
        oldest_pending_age_seconds=pending_age,
        replay_deliveries=int(replay_deliveries or 0),
    )
