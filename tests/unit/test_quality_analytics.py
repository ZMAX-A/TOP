from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from testops.api.quality_services import (
    _detect_flaky_cases,
    _quality_alert_status,
    _quality_change_signal,
)


class QualityChangeSignalTests(unittest.TestCase):
    def test_classifies_drops_and_missing_comparisons(self) -> None:
        stable = _quality_change_signal(
            "RUN_PASS_RATE",
            97.0,
            96.0,
            warning_drop_points=5,
            critical_drop_points=10,
        )
        warning = _quality_change_signal(
            "CASE_PASS_RATE",
            89.0,
            95.0,
            warning_drop_points=5,
            critical_drop_points=10,
        )
        critical = _quality_change_signal(
            "EXECUTION_RELIABILITY",
            82.5,
            95.0,
            warning_drop_points=5,
            critical_drop_points=10,
        )
        missing = _quality_change_signal(
            "RUN_PASS_RATE",
            None,
            95.0,
            warning_drop_points=5,
            critical_drop_points=10,
        )

        self.assertEqual(stable.alert_status, "STABLE")
        self.assertEqual(stable.delta_percentage_points, 1.0)
        self.assertEqual(warning.alert_status, "WARNING")
        self.assertEqual(warning.delta_percentage_points, -6.0)
        self.assertEqual(critical.alert_status, "CRITICAL")
        self.assertEqual(critical.delta_percentage_points, -12.5)
        self.assertEqual(missing.alert_status, "NO_DATA")
        self.assertIsNone(missing.delta_percentage_points)
        self.assertEqual(
            _quality_alert_status((stable, warning, critical, missing)),
            "CRITICAL",
        )
        self.assertEqual(_quality_alert_status((missing,)), "NO_DATA")


class FlakyCaseDetectionTests(unittest.TestCase):
    def test_requires_repeated_pass_fail_transitions(self) -> None:
        moment = datetime(2026, 8, 14, 8, tzinfo=UTC)
        flaky_case_id = uuid4()
        changed_once_case_id = uuid4()
        sparse_case_id = uuid4()
        rows = [
            (flaky_case_id, "TC-FLAKY-001", "PASSED", moment - timedelta(days=2), uuid4()),
            (flaky_case_id, "TC-FLAKY-001", "FAILED", moment - timedelta(days=1), uuid4()),
            (flaky_case_id, "TC-FLAKY-001", "PASSED", moment, uuid4()),
            (flaky_case_id, "TC-FLAKY-001", "INFRA_ERROR", moment, uuid4()),
            (
                changed_once_case_id,
                "TC-REGRESSION-001",
                "PASSED",
                moment - timedelta(days=2),
                uuid4(),
            ),
            (
                changed_once_case_id,
                "TC-REGRESSION-001",
                "PASSED",
                moment - timedelta(days=1),
                uuid4(),
            ),
            (changed_once_case_id, "TC-REGRESSION-001", "FAILED", moment, uuid4()),
            (sparse_case_id, "TC-SPARSE-001", "PASSED", moment - timedelta(days=1), uuid4()),
            (sparse_case_id, "TC-SPARSE-001", "FAILED", moment, uuid4()),
        ]

        cases, detected_count = _detect_flaky_cases(rows)

        self.assertEqual(detected_count, 1)
        self.assertEqual(len(cases), 1)
        flaky = cases[0]
        self.assertEqual(flaky.case_id, flaky_case_id)
        self.assertEqual(flaky.case_code, "TC-FLAKY-001")
        self.assertEqual(flaky.conclusive_executions, 3)
        self.assertEqual(flaky.passed_executions, 2)
        self.assertEqual(flaky.failed_executions, 1)
        self.assertEqual(flaky.pass_rate_percent, 66.67)
        self.assertEqual(flaky.status_transitions, 2)
        self.assertEqual(flaky.transition_rate_percent, 100.0)
        self.assertEqual(flaky.latest_status, "PASSED")
        self.assertEqual(flaky.latest_completed_at, moment)


if __name__ == "__main__":
    unittest.main()
