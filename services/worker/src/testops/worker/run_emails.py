"""SMTP delivery for durable Run result email notifications."""

from __future__ import annotations

import asyncio
import html
import os
import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from testops.api.config import Settings
from testops.api.persistence import RunEmailDeliveryRecord


class RunEmailDeliveryError(RuntimeError):
    """Email delivery failed without exposing SMTP credentials or recipient data."""


class RunEmailSender(Protocol):
    async def send(self, delivery: RunEmailDeliveryRecord) -> None:
        """Send one persisted delivery or raise a sanitized delivery error."""


@dataclass(frozen=True, slots=True)
class RunEmailDispatchSummary:
    selected: int
    delivered: int
    retrying: int
    failed: int


@dataclass(frozen=True, slots=True)
class RunEmailDispatcherSettings:
    batch_size: int = 20
    max_attempts: int = 5

    @classmethod
    def from_environment(cls) -> RunEmailDispatcherSettings:
        batch_size = int(os.getenv("RUN_EMAIL_BATCH_SIZE", "20"))
        max_attempts = int(os.getenv("RUN_EMAIL_MAX_ATTEMPTS", "5"))
        if not 1 <= batch_size <= 500:
            raise ValueError("RUN_EMAIL_BATCH_SIZE must be between 1 and 500")
        if not 1 <= max_attempts <= 20:
            raise ValueError("RUN_EMAIL_MAX_ATTEMPTS must be between 1 and 20")
        return cls(batch_size=batch_size, max_attempts=max_attempts)


_STATUS_LABELS = {
    "PASSED": "通过",
    "FAILED": "失败",
    "CANCELED": "已取消",
    "TIMED_OUT": "超时",
    "INFRA_ERROR": "基础设施异常",
}


def _single_line(value: object, *, fallback: str) -> str:
    resolved = " ".join(str(value or fallback).splitlines()).strip()
    return resolved or fallback


def _result_lines(payload: dict[str, object], public_base_url: str) -> tuple[str, list[str], str]:
    if payload.get("kind") == "test":
        subject = "[TestOps] 邮件通知测试成功"
        lines = [
            "这是一封 TestOps 邮件通知测试。",
            "收到此邮件说明平台 SMTP 配置和您的收件地址均可用。",
        ]
        return subject, lines, public_base_url

    project = payload.get("project") if isinstance(payload.get("project"), dict) else {}
    run = payload.get("run") if isinstance(payload.get("run"), dict) else {}
    project_name = _single_line(project.get("name"), fallback="未知项目")
    status = _single_line(run.get("status"), fallback="UNKNOWN")
    status_label = _STATUS_LABELS.get(status, status)
    run_id = _single_line(run.get("id"), fallback="unknown")
    case_count = int(run.get("case_count") or 0)
    counts = run.get("status_counts") if isinstance(run.get("status_counts"), dict) else {}
    count_summary = (
        "，".join(
            f"{_STATUS_LABELS.get(str(key), str(key))} {value}" for key, value in counts.items()
        )
        or "暂无分项结果"
    )
    duration_ms = run.get("duration_ms")
    duration_text = "—" if duration_ms is None else f"{int(duration_ms) / 1000:.1f} 秒"
    baseline_version = _single_line(run.get("baseline_version"), fallback="—")
    package_version = _single_line(run.get("package_version"), fallback="—")
    finished_at = _single_line(run.get("finished_at"), fallback="—")
    project_id = _single_line(project.get("id"), fallback="unknown")
    run_url = f"{public_base_url}/projects/{project_id}/runs/{run_id}"
    subject = f"[TestOps][{status_label}] {project_name} · {run_id[:8]}"
    lines = [
        f"项目：{project_name}",
        f"运行状态：{status_label}",
        f"Run ID：{run_id}",
        f"用例总数：{case_count}",
        f"结果统计：{count_summary}",
        f"执行耗时：{duration_text}",
        f"用例基线：{baseline_version}",
        f"自动化包：{package_version}",
        f"完成时间：{finished_at}",
    ]
    return subject, lines, run_url


class SmtpRunEmailSender:
    def __init__(self, settings: Settings):
        self._settings = settings

    def _message(self, delivery: RunEmailDeliveryRecord) -> EmailMessage:
        settings = self._settings
        if not settings.smtp_configured:
            raise RunEmailDeliveryError("SMTP delivery is not configured")
        assert settings.smtp_from_address is not None
        subject, lines, link = _result_lines(delivery.payload, settings.public_base_url)
        message = EmailMessage()
        message["Subject"] = _single_line(subject, fallback="[TestOps] Run notification")
        message["From"] = formataddr((settings.smtp_from_name, settings.smtp_from_address))
        message["To"] = delivery.recipient_email
        message["X-TestOps-Delivery-ID"] = str(delivery.id)
        text_body = "\n".join([*lines, "", f"查看详情：{link}", "", "此邮件由 TestOps 自动发送。"])
        message.set_content(text_body)
        html_lines = "".join(f"<li>{html.escape(line)}</li>" for line in lines)
        message.add_alternative(
            "<!doctype html><html><body>"
            "<div style='max-width:640px;margin:auto;font-family:Arial,sans-serif;color:#24313d'>"
            "<h2 style='color:#0f8f7c'>TestOps 测试运行通知</h2>"
            f"<ul style='line-height:1.9'>{html_lines}</ul>"
            f"<p><a href='{html.escape(link, quote=True)}' "
            "style='color:#0f8f7c'>查看运行详情</a></p>"
            "<p style='color:#83909c;font-size:12px'>此邮件由 TestOps 自动发送。</p>"
            "</div></body></html>",
            subtype="html",
        )
        return message

    def _send_sync(self, delivery: RunEmailDeliveryRecord) -> None:
        settings = self._settings
        if not settings.smtp_configured:
            raise RunEmailDeliveryError("SMTP delivery is not configured")
        assert settings.smtp_host is not None
        message = self._message(delivery)
        connection_type = smtplib.SMTP_SSL if settings.smtp_use_ssl else smtplib.SMTP
        try:
            with connection_type(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
            ) as client:
                client.ehlo()
                if settings.smtp_starttls:
                    client.starttls()
                    client.ehlo()
                if settings.smtp_username is not None:
                    assert settings.smtp_password is not None
                    client.login(settings.smtp_username, settings.smtp_password)
                client.send_message(message)
        except RunEmailDeliveryError:
            raise
        except Exception as exc:
            raise RunEmailDeliveryError(f"SMTP delivery failed ({type(exc).__name__})") from exc

    async def send(self, delivery: RunEmailDeliveryRecord) -> None:
        await asyncio.to_thread(self._send_sync, delivery)


def _retry_at(moment: datetime, attempts: int) -> datetime:
    return moment + timedelta(seconds=min(300, 2 ** min(attempts, 8)))


async def dispatch_run_email_batch(
    session_factory: async_sessionmaker[AsyncSession],
    sender: RunEmailSender,
    *,
    limit: int = 20,
    max_attempts: int = 5,
    now: datetime | None = None,
) -> RunEmailDispatchSummary:
    if not 1 <= limit <= 500:
        raise ValueError("Run email dispatch limit must be between 1 and 500")
    if not 1 <= max_attempts <= 20:
        raise ValueError("Run email maximum attempts must be between 1 and 20")
    moment = now or datetime.now(UTC)
    delivered_count = 0
    retrying = 0
    failed = 0
    async with session_factory() as session:
        async with session.begin():
            deliveries = tuple(
                await session.scalars(
                    select(RunEmailDeliveryRecord)
                    .where(
                        RunEmailDeliveryRecord.status == "PENDING",
                        RunEmailDeliveryRecord.available_at <= moment,
                    )
                    .order_by(RunEmailDeliveryRecord.created_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for delivery in deliveries:
                delivery.attempts += 1
                try:
                    await sender.send(delivery)
                except RunEmailDeliveryError as exc:
                    delivery.last_error = str(exc)[:500]
                    if delivery.attempts >= max_attempts:
                        delivery.status = "FAILED"
                        failed += 1
                    else:
                        delivery.available_at = _retry_at(moment, delivery.attempts)
                        retrying += 1
                except Exception as exc:  # defensive boundary around SMTP implementations
                    delivery.last_error = f"Unexpected email sender error ({type(exc).__name__})"
                    if delivery.attempts >= max_attempts:
                        delivery.status = "FAILED"
                        failed += 1
                    else:
                        delivery.available_at = _retry_at(moment, delivery.attempts)
                        retrying += 1
                else:
                    delivery.status = "DELIVERED"
                    delivery.delivered_at = moment
                    delivery.last_error = None
                    delivered_count += 1
    return RunEmailDispatchSummary(
        selected=delivered_count + retrying + failed,
        delivered=delivered_count,
        retrying=retrying,
        failed=failed,
    )
