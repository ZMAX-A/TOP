from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from testops.api.governance import create_candidate_run, create_validation_run
from testops.api.governance_schemas import CandidateRunCreate


class CoordinatedReleaseRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_full_regression_accepts_verified_draft_package_path(self) -> None:
        request = SimpleNamespace(status="CANDIDATE", candidate_baseline_id=uuid4())
        payload = CandidateRunCreate(
            target_id=uuid4(),
            environment_id=uuid4(),
            automation_package_id=uuid4(),
        )
        expected = (SimpleNamespace(id=uuid4()), True)

        with (
            patch(
                "testops.api.governance.get_change_request",
                AsyncMock(return_value=request),
            ),
            patch(
                "testops.api.governance.create_run",
                AsyncMock(return_value=expected),
            ) as create_run_mock,
        ):
            actual = await create_candidate_run(
                Mock(),
                uuid4(),
                uuid4(),
                payload,
                idempotency_key="coordinated-regression",
                actor_id=uuid4(),
            )

        self.assertEqual(actual, expected)
        self.assertEqual(
            create_run_mock.await_args.kwargs["allowed_package_statuses"],
            frozenset({"ACTIVE", "DRAFT"}),
        )

    async def test_affected_validation_accepts_verified_draft_package_path(self) -> None:
        request = SimpleNamespace(
            status="DRAFT",
            candidate_baseline_id=uuid4(),
            validation_run_id=None,
            validation_status="PENDING_EXECUTION",
        )
        payload = CandidateRunCreate(
            target_id=uuid4(),
            environment_id=uuid4(),
            automation_package_id=uuid4(),
        )
        run = SimpleNamespace(id=uuid4(), case_count=1)
        session = SimpleNamespace(add=Mock(), commit=AsyncMock())
        changed_item = SimpleNamespace(
            change_type="MODIFY",
            case_code="TC-LOGIN-001",
            after_document={"enabled": True},
        )

        with (
            patch(
                "testops.api.governance.get_change_request",
                AsyncMock(return_value=request),
            ),
            patch(
                "testops.api.governance.change_request_items",
                AsyncMock(return_value=(changed_item,)),
            ),
            patch(
                "testops.api.governance.create_run",
                AsyncMock(return_value=(run, True)),
            ) as create_run_mock,
        ):
            actual = await create_validation_run(
                session,
                uuid4(),
                uuid4(),
                payload,
                idempotency_key="coordinated-validation",
                actor_id=uuid4(),
            )

        self.assertEqual(actual, (run, True))
        self.assertEqual(
            create_run_mock.await_args.kwargs["allowed_package_statuses"],
            frozenset({"ACTIVE", "DRAFT"}),
        )
        self.assertEqual(request.validation_run_id, run.id)
        self.assertEqual(request.validation_status, "QUEUED")
        session.commit.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
