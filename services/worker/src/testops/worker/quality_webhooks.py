"""Durable quality Webhook delivery with signing and bounded retries."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from testops.api.persistence import (
    QualityWebhookConfigRecord,
    QualityWebhookDeliveryRecord,
)


class WebhookDeliveryError(RuntimeError):
    """A delivery failed but may succeed after configuration or network recovery."""


class PermanentWebhookDeliveryError(WebhookDeliveryError):
    """A delivery cannot succeed without changing the destination or request."""


class QualityWebhookSender(Protocol):
    async def send(
        self,
        config: QualityWebhookConfigRecord,
        delivery: QualityWebhookDeliveryRecord,
    ) -> int:
        """Return the HTTP response status without exposing a response body."""


DestinationValidator = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class QualityWebhookDispatchSummary:
    selected: int
    delivered: int
    retrying: int
    failed: int


@dataclass(frozen=True, slots=True)
class QualityWebhookDispatcherSettings:
    batch_size: int = 20
    timeout_seconds: float = 10.0
    max_attempts: int = 8
    allow_private_networks: bool = False

    @classmethod
    def from_environment(cls) -> QualityWebhookDispatcherSettings:
        batch_size = int(os.getenv("QUALITY_WEBHOOK_BATCH_SIZE", "20"))
        timeout_seconds = float(os.getenv("QUALITY_WEBHOOK_TIMEOUT_SECONDS", "10"))
        max_attempts = int(os.getenv("QUALITY_WEBHOOK_MAX_ATTEMPTS", "8"))
        allow_private = os.getenv("QUALITY_WEBHOOK_ALLOW_PRIVATE_NETWORKS", "false").strip().lower()
        if not 1 <= batch_size <= 500:
            raise ValueError("QUALITY_WEBHOOK_BATCH_SIZE must be between 1 and 500")
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("QUALITY_WEBHOOK_TIMEOUT_SECONDS must be between 1 and 60")
        if not 1 <= max_attempts <= 20:
            raise ValueError("QUALITY_WEBHOOK_MAX_ATTEMPTS must be between 1 and 20")
        if allow_private not in {"0", "1", "false", "true", "no", "yes", "off", "on"}:
            raise ValueError("QUALITY_WEBHOOK_ALLOW_PRIVATE_NETWORKS must be a boolean")
        return cls(
            batch_size=batch_size,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            allow_private_networks=allow_private in {"1", "true", "yes", "on"},
        )


class HttpxQualityWebhookSender:
    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        allow_private_networks: bool = False,
        environ: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        destination_validator: DestinationValidator | None = None,
    ):
        self._timeout_seconds = timeout_seconds
        self._allow_private_networks = allow_private_networks
        self._environ = os.environ if environ is None else environ
        self._transport = transport
        self._destination_validator = destination_validator

    async def _validate_destination(self, endpoint_url: str) -> None:
        if self._destination_validator is not None:
            await self._destination_validator(endpoint_url)
            return
        parsed = urlsplit(endpoint_url)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise PermanentWebhookDeliveryError("Webhook destination must use HTTPS")
        try:
            port = parsed.port
        except ValueError as exc:
            raise PermanentWebhookDeliveryError("Webhook destination has an invalid port") from exc
        if parsed.username or parsed.password or parsed.fragment or port not in {None, 443}:
            raise PermanentWebhookDeliveryError("Webhook destination is not allowed")
        if self._allow_private_networks:
            return
        try:
            address_rows = await asyncio.to_thread(
                socket.getaddrinfo,
                parsed.hostname,
                443,
                0,
                socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise WebhookDeliveryError("Webhook destination DNS resolution failed") from exc
        if not address_rows:
            raise WebhookDeliveryError("Webhook destination DNS resolution returned no addresses")
        for row in address_rows:
            try:
                address = ip_address(row[4][0])
            except ValueError as exc:
                raise PermanentWebhookDeliveryError(
                    "Webhook destination resolved to an invalid address"
                ) from exc
            if not address.is_global:
                raise PermanentWebhookDeliveryError(
                    "Webhook destination resolved to a private or reserved address"
                )

    def _signing_secret(self, config: QualityWebhookConfigRecord) -> str | None:
        if config.signing_secret_name is None:
            return None
        environment_name = f"TESTOPS_SECRET_{config.signing_secret_name}"
        value = self._environ.get(environment_name, "")
        if not value:
            raise WebhookDeliveryError("Webhook signing secret is not configured in the process")
        return value

    async def send(
        self,
        config: QualityWebhookConfigRecord,
        delivery: QualityWebhookDeliveryRecord,
    ) -> int:
        await self._validate_destination(config.endpoint_url)
        body = json.dumps(
            delivery.payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        timestamp = str(int(datetime.now(UTC).timestamp()))
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "TestOps-Quality-Webhook/1.0",
            "X-TestOps-Delivery": str(delivery.id),
            "X-TestOps-Event": delivery.event_type,
            "X-TestOps-Timestamp": timestamp,
        }
        secret = self._signing_secret(config)
        if secret is not None:
            signing_input = timestamp.encode("ascii") + b"." + body
            digest = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).hexdigest()
            headers["X-TestOps-Signature-256"] = f"sha256={digest}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                follow_redirects=False,
                transport=self._transport,
                trust_env=False,
            ) as client:
                response = await client.post(config.endpoint_url, content=body, headers=headers)
        except httpx.TransportError as exc:
            raise WebhookDeliveryError("Webhook transport failed") from exc
        return response.status_code


def _is_retryable_status(status_code: int) -> bool:
    return status_code in {408, 425, 429} or status_code >= 500


def _retry_at(moment: datetime, attempts: int) -> datetime:
    return moment + timedelta(seconds=min(300, 2 ** min(attempts, 8)))


async def dispatch_quality_webhook_batch(
    session_factory: async_sessionmaker[AsyncSession],
    sender: QualityWebhookSender,
    *,
    limit: int = 20,
    max_attempts: int = 8,
    now: datetime | None = None,
) -> QualityWebhookDispatchSummary:
    if not 1 <= limit <= 500:
        raise ValueError("Webhook dispatch limit must be between 1 and 500")
    if not 1 <= max_attempts <= 20:
        raise ValueError("Webhook maximum attempts must be between 1 and 20")
    moment = now or datetime.now(UTC)
    delivered = 0
    retrying = 0
    failed = 0
    async with session_factory() as session:
        async with session.begin():
            deliveries = tuple(
                await session.scalars(
                    select(QualityWebhookDeliveryRecord)
                    .where(
                        QualityWebhookDeliveryRecord.status == "PENDING",
                        QualityWebhookDeliveryRecord.available_at <= moment,
                    )
                    .order_by(QualityWebhookDeliveryRecord.created_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for delivery in deliveries:
                delivery.attempts += 1
                config = await session.get(
                    QualityWebhookConfigRecord,
                    delivery.webhook_config_id,
                )
                if config is None or not config.enabled:
                    failed += 1
                    delivery.status = "FAILED"
                    delivery.last_error = "Webhook configuration is missing or disabled"
                    continue
                try:
                    response_status = await sender.send(config, delivery)
                except PermanentWebhookDeliveryError as exc:
                    failed += 1
                    delivery.status = "FAILED"
                    delivery.last_error = str(exc)[:500]
                except WebhookDeliveryError as exc:
                    delivery.last_error = str(exc)[:500]
                    if delivery.attempts >= max_attempts:
                        failed += 1
                        delivery.status = "FAILED"
                    else:
                        retrying += 1
                        delivery.available_at = _retry_at(moment, delivery.attempts)
                except Exception as exc:  # defensive boundary around third-party transports
                    delivery.last_error = f"Unexpected Webhook sender error ({type(exc).__name__})"
                    if delivery.attempts >= max_attempts:
                        failed += 1
                        delivery.status = "FAILED"
                    else:
                        retrying += 1
                        delivery.available_at = _retry_at(moment, delivery.attempts)
                else:
                    delivery.response_status = response_status
                    if 200 <= response_status < 300:
                        delivered += 1
                        delivery.status = "DELIVERED"
                        delivery.delivered_at = moment
                        delivery.last_error = None
                    elif _is_retryable_status(response_status) and delivery.attempts < max_attempts:
                        retrying += 1
                        delivery.last_error = f"Webhook endpoint returned HTTP {response_status}"
                        delivery.available_at = _retry_at(moment, delivery.attempts)
                    else:
                        failed += 1
                        delivery.status = "FAILED"
                        delivery.last_error = f"Webhook endpoint returned HTTP {response_status}"
    return QualityWebhookDispatchSummary(
        selected=delivered + retrying + failed,
        delivered=delivered,
        retrying=retrying,
        failed=failed,
    )
