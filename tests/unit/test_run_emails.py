from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select

from testops.api.config import Settings
from testops.api.database import create_database_runtime, create_schema
from testops.api.identity_schemas import RunEmailPreferenceUpdate
from testops.api.persistence import (
    AuditLogRecord,
    ProjectRecord,
    RunEmailDeliveryRecord,
    UserRecord,
)
from testops.api.persistence import (
    TestRunRecord as RunRecord,
)
from testops.api.run_email_services import (
    enqueue_run_email_test,
    enqueue_run_result_email,
    get_run_email_preference,
    update_run_email_preference,
)
from testops.api.services import ResourceConflict
from testops.worker.run_emails import SmtpRunEmailSender, dispatch_run_email_batch


class RunEmailPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine, self.session_factory = create_database_runtime(
            Settings(database_url="sqlite+aiosqlite:///:memory:")
        )
        await create_schema(self.engine)
        self.user_id = uuid4()
        self.project_id = uuid4()
        async with self.session_factory() as session:
            session.add_all(
                (
                    UserRecord(
                        id=self.user_id,
                        username="email-user",
                        display_name="Email User",
                        password_hash="unit-test-hash",
                        system_role="USER",
                        status="ACTIVE",
                    ),
                    ProjectRecord(
                        id=self.project_id,
                        key="email-project",
                        name="Email Project",
                        description=None,
                    ),
                )
            )
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_user_can_save_address_and_select_terminal_statuses(self) -> None:
        payload = RunEmailPreferenceUpdate(
            emails=("Owner@Example.COM", "Backup@Example.COM"),
            selected_emails=("Backup@Example.COM",),
            enabled=True,
            statuses=("PASSED", "FAILED"),
        )
        async with self.session_factory() as session:
            preference = await update_run_email_preference(
                session,
                self.user_id,
                payload,
                smtp_configured=True,
            )
            audit = await session.scalar(
                select(AuditLogRecord).where(
                    AuditLogRecord.action == "identity.run_email_preference_updated"
                )
            )

        self.assertEqual(preference.email, "Owner@example.com")
        self.assertEqual(preference.emails, ("Owner@example.com", "Backup@example.com"))
        self.assertEqual(preference.selected_emails, ("Backup@example.com",))
        self.assertTrue(preference.enabled)
        self.assertEqual([status.value for status in preference.statuses], ["PASSED", "FAILED"])
        self.assertIsNotNone(audit)
        self.assertNotIn("Owner@example.com", str(audit.details))
        self.assertEqual(audit.details["after"]["recipient_count"], 2)
        self.assertEqual(audit.details["after"]["selected_recipient_count"], 1)

    async def test_preference_rejects_more_than_five_or_duplicate_recipients(self) -> None:
        with self.assertRaisesRegex(ValidationError, "at most 5"):
            RunEmailPreferenceUpdate(
                emails=tuple(f"owner{index}@example.com" for index in range(6)),
                enabled=True,
                statuses=("FAILED",),
            )
        with self.assertRaisesRegex(ValidationError, "must be unique"):
            RunEmailPreferenceUpdate(
                emails=("Owner@example.com", "owner@EXAMPLE.COM"),
                enabled=True,
                statuses=("FAILED",),
            )
        with self.assertRaisesRegex(ValidationError, "configured recipient"):
            RunEmailPreferenceUpdate(
                emails=("owner@example.com",),
                selected_emails=("other@example.com",),
                enabled=True,
                statuses=("FAILED",),
            )

    async def test_user_cannot_enable_notifications_before_smtp_is_configured(self) -> None:
        payload = RunEmailPreferenceUpdate(
            emails=("owner@example.com",),
            enabled=True,
            statuses=("FAILED",),
        )
        async with self.session_factory() as session:
            with self.assertRaisesRegex(ResourceConflict, "SMTP"):
                await update_run_email_preference(
                    session,
                    self.user_id,
                    payload,
                    smtp_configured=False,
                )

    async def test_disabling_preference_cancels_pending_delivery(self) -> None:
        async with self.session_factory() as session:
            await update_run_email_preference(
                session,
                self.user_id,
                RunEmailPreferenceUpdate(
                    emails=("owner@example.com", "backup@example.com"),
                    enabled=True,
                    statuses=("FAILED",),
                ),
                smtp_configured=True,
            )
            await enqueue_run_email_test(
                session,
                self.user_id,
                smtp_configured=True,
            )
            await update_run_email_preference(
                session,
                self.user_id,
                RunEmailPreferenceUpdate(
                    emails=("owner@example.com", "backup@example.com"),
                    enabled=False,
                    statuses=("FAILED",),
                ),
                smtp_configured=True,
            )
            stored = tuple(
                await session.scalars(
                    select(RunEmailDeliveryRecord).where(
                        RunEmailDeliveryRecord.event_type == "run.email.test"
                    )
                )
            )

        self.assertEqual(len(stored), 2)
        self.assertTrue(all(item.status == "CANCELED" for item in stored))

    async def test_result_enqueue_respects_selected_status_and_masks_destination(self) -> None:
        async with self.session_factory() as session:
            await update_run_email_preference(
                session,
                self.user_id,
                RunEmailPreferenceUpdate(
                    emails=("owner@example.com", "backup@example.com"),
                    selected_emails=("backup@example.com",),
                    enabled=True,
                    statuses=("FAILED",),
                ),
                smtp_configured=True,
            )
            finished_at = datetime.now(UTC)
            record = RunRecord(
                id=uuid4(),
                project_id=self.project_id,
                target_id=uuid4(),
                environment_id=uuid4(),
                baseline_id=uuid4(),
                automation_package_id=uuid4(),
                status="FAILED",
                idempotency_key="email-result-test",
                request_hash="sha256:" + "1" * 64,
                snapshot_digest="sha256:" + "2" * 64,
                snapshot={
                    "case_baseline": {"version": "case-v1.0.6"},
                    "automation_package": {"version": "v1.1.12"},
                },
                result_document={
                    "case_results": [
                        {"status": "PASSED"},
                        {"status": "FAILED"},
                    ]
                },
                case_count=2,
                timeout_seconds=3600,
                created_by=self.user_id,
                started_at=finished_at - timedelta(seconds=5),
                finished_at=finished_at,
            )
            deliveries = await enqueue_run_result_email(session, record)
            self.assertEqual(len(deliveries), 1)
            self.assertEqual(
                {delivery.destination_display for delivery in deliveries},
                {"b***@example.com"},
            )
            self.assertEqual(
                deliveries[0].payload["run"]["status_counts"],
                {"FAILED": 1, "PASSED": 1},
            )
            self.assertEqual(len({delivery.dedupe_key for delivery in deliveries}), 1)
            for delivery in deliveries:
                session.expunge(delivery)

            record.status = "PASSED"
            self.assertEqual(await enqueue_run_result_email(session, record), ())

    async def test_dispatch_marks_email_delivered(self) -> None:
        async with self.session_factory() as session:
            await update_run_email_preference(
                session,
                self.user_id,
                RunEmailPreferenceUpdate(
                    email="owner@example.com",
                    enabled=False,
                    statuses=("FAILED",),
                ),
                smtp_configured=True,
            )
            queued = await enqueue_run_email_test(
                session,
                self.user_id,
                smtp_configured=True,
            )

        class Sender:
            async def send(self, _delivery: RunEmailDeliveryRecord) -> None:
                return None

        summary = await dispatch_run_email_batch(self.session_factory, Sender())
        async with self.session_factory() as session:
            stored = await session.get(RunEmailDeliveryRecord, queued.id)

        self.assertEqual(summary.delivered, 1)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, "DELIVERED")

    async def test_empty_preference_uses_all_terminal_statuses(self) -> None:
        async with self.session_factory() as session:
            preference = await get_run_email_preference(
                session,
                self.user_id,
                smtp_configured=False,
            )
        self.assertFalse(preference.enabled)
        self.assertEqual(preference.emails, ())
        self.assertEqual(preference.selected_emails, ())
        self.assertEqual(len(preference.statuses), 5)


def test_smtp_sender_builds_plain_and_html_run_message_without_logging_recipient() -> None:
    settings = Settings(
        smtp_host="smtp.example.com",
        smtp_port=25,
        smtp_from_address="testops@example.com",
        smtp_from_name="TestOps",
        smtp_starttls=False,
        public_base_url="https://testops.example.com",
    )
    delivery = RunEmailDeliveryRecord(
        id=uuid4(),
        user_id=uuid4(),
        project_id=uuid4(),
        run_id=uuid4(),
        event_type="run.result",
        dedupe_key="smtp-unit-test",
        recipient_email="owner@example.com",
        destination_display="o***@example.com",
        payload={
            "kind": "run_result",
            "project": {"id": str(uuid4()), "name": "Web 自动化"},
            "run": {
                "id": str(uuid4()),
                "status": "FAILED",
                "case_count": 2,
                "status_counts": {"PASSED": 1, "FAILED": 1},
            },
        },
        status="PENDING",
        attempts=0,
        available_at=datetime.now(UTC),
    )
    smtp = Mock()
    smtp.__enter__ = Mock(return_value=smtp)
    smtp.__exit__ = Mock(return_value=False)
    with patch("testops.worker.run_emails.smtplib.SMTP", return_value=smtp):
        asyncio.run(SmtpRunEmailSender(settings).send(delivery))

    message = smtp.send_message.call_args.args[0]
    assert message["To"] == "owner@example.com"
    assert "失败" in str(message["Subject"])
    assert "查看运行详情" in message.get_body(preferencelist=("html",)).get_content()
