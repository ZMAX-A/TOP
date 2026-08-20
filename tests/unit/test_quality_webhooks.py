from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx

from testops.api.persistence import (
    QualityWebhookConfigRecord,
    QualityWebhookDeliveryRecord,
)
from testops.api.quality_alert_services import decide_quality_alert_event
from testops.worker.quality_webhooks import (
    HttpxQualityWebhookSender,
    PermanentWebhookDeliveryError,
    WebhookDeliveryError,
)


def _records() -> tuple[QualityWebhookConfigRecord, QualityWebhookDeliveryRecord]:
    project_id = uuid4()
    config = QualityWebhookConfigRecord(
        id=uuid4(),
        project_id=project_id,
        enabled=True,
        endpoint_url="https://hooks.example.com/testops/token",
        minimum_alert_status="WARNING",
        signing_secret_name="QUALITY_WEBHOOK_TEST",
        signing_secret_ref="secret://quality/webhook/test",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    delivery = QualityWebhookDeliveryRecord(
        id=uuid4(),
        project_id=project_id,
        webhook_config_id=config.id,
        event_type="quality.alert.test",
        dedupe_key=f"test:{uuid4()}",
        destination_display="https://hooks.example.com/***",
        payload={"event_type": "quality.alert.test", "message": "hello"},
        status="PENDING",
        attempts=0,
        available_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    return config, delivery


def test_http_sender_uses_canonical_body_and_hmac_signature() -> None:
    config, delivery = _records()
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        captured["headers"] = dict(request.headers)
        return httpx.Response(204)

    async def allow_destination(_endpoint_url: str) -> None:
        return None

    sender = HttpxQualityWebhookSender(
        environ={"TESTOPS_SECRET_QUALITY_WEBHOOK_TEST": "signing-secret"},
        transport=httpx.MockTransport(handler),
        destination_validator=allow_destination,
    )
    status_code = asyncio.run(sender.send(config, delivery))
    assert status_code == 204
    assert captured["url"] == config.endpoint_url
    expected_body = json.dumps(
        delivery.payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert captured["body"] == expected_body
    headers = captured["headers"]
    assert isinstance(headers, dict)
    timestamp = str(headers["x-testops-timestamp"])
    expected_signature = hmac.new(
        b"signing-secret",
        timestamp.encode("ascii") + b"." + expected_body,
        hashlib.sha256,
    ).hexdigest()
    assert headers["x-testops-signature-256"] == f"sha256={expected_signature}"
    assert headers["x-testops-delivery"] == str(delivery.id)
    assert headers["x-testops-event"] == delivery.event_type


def test_http_sender_rejects_private_destination_before_transport() -> None:
    config, delivery = _records()
    config.endpoint_url = "https://127.0.0.1/webhook"
    sender = HttpxQualityWebhookSender(transport=httpx.MockTransport(lambda _: httpx.Response(204)))
    try:
        asyncio.run(sender.send(config, delivery))
    except PermanentWebhookDeliveryError as exc:
        assert "private or reserved" in str(exc)
    else:
        raise AssertionError("private Webhook destination was not rejected")


def test_http_sender_does_not_expose_missing_signing_secret_metadata() -> None:
    config, delivery = _records()

    async def allow_destination(_endpoint_url: str) -> None:
        return None

    sender = HttpxQualityWebhookSender(
        environ={},
        transport=httpx.MockTransport(lambda _: httpx.Response(204)),
        destination_validator=allow_destination,
    )
    try:
        asyncio.run(sender.send(config, delivery))
    except WebhookDeliveryError as exc:
        message = str(exc)
        assert "signing secret is not configured" in message
        assert "QUALITY_WEBHOOK_TEST" not in message
        assert "secret://" not in message
    else:
        raise AssertionError("missing signing secret was not rejected")


def test_quality_alert_decision_handles_active_transitions_without_cooldown_delay() -> None:
    moment = datetime.now(UTC)
    escalated = decide_quality_alert_event(
        current_status="CRITICAL",
        active_notification_status="WARNING",
        minimum_alert_status="WARNING",
        cooldown_until=moment + timedelta(hours=1),
        now=moment,
    )
    assert escalated.event_type == "quality.alert.escalated"
    assert escalated.active_notification_status == "CRITICAL"

    deescalated = decide_quality_alert_event(
        current_status="WARNING",
        active_notification_status="CRITICAL",
        minimum_alert_status="WARNING",
        cooldown_until=moment + timedelta(hours=1),
        now=moment,
    )
    assert deescalated.event_type == "quality.alert.deescalated"
    assert deescalated.active_notification_status == "WARNING"

    deescalated_below_subscription = decide_quality_alert_event(
        current_status="WARNING",
        active_notification_status="CRITICAL",
        minimum_alert_status="CRITICAL",
        cooldown_until=moment + timedelta(hours=1),
        now=moment,
    )
    assert deescalated_below_subscription.event_type == "quality.alert.deescalated"
    assert deescalated_below_subscription.active_notification_status is None

    insufficient = decide_quality_alert_event(
        current_status="NO_DATA",
        active_notification_status="CRITICAL",
        minimum_alert_status="WARNING",
        cooldown_until=moment + timedelta(hours=1),
        now=moment,
    )
    assert insufficient.event_type is None
    assert insufficient.active_notification_status == "CRITICAL"
    assert insufficient.reason == "insufficient_data"

    recovered = decide_quality_alert_event(
        current_status="STABLE",
        active_notification_status="CRITICAL",
        minimum_alert_status="WARNING",
        cooldown_until=moment + timedelta(hours=1),
        now=moment,
    )
    assert recovered.event_type == "quality.alert.recovered"
    assert recovered.active_notification_status is None


def test_quality_alert_decision_suppresses_retrigger_until_cooldown_expires() -> None:
    moment = datetime.now(UTC)
    suppressed = decide_quality_alert_event(
        current_status="WARNING",
        active_notification_status=None,
        minimum_alert_status="WARNING",
        cooldown_until=moment + timedelta(minutes=5),
        now=moment,
    )
    assert suppressed.event_type is None
    assert suppressed.reason == "cooldown_active"

    triggered = decide_quality_alert_event(
        current_status="WARNING",
        active_notification_status=None,
        minimum_alert_status="WARNING",
        cooldown_until=moment - timedelta(seconds=1),
        now=moment,
    )
    assert triggered.event_type == "quality.alert.triggered"
    assert triggered.active_notification_status == "WARNING"
