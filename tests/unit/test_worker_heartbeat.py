"""Worker heartbeat scheduling remains independent from Celery task execution."""

from __future__ import annotations

import unittest
from importlib import import_module
from unittest.mock import Mock, patch

celery_application = import_module("testops.worker.celery_app")


class WorkerHeartbeatTests(unittest.TestCase):
    def test_background_loop_reports_until_stopped(self) -> None:
        stop_event = Mock()
        stop_event.wait.side_effect = [False, True]

        with patch.object(celery_application, "_report_runner_heartbeat") as report:
            celery_application._runner_heartbeat_loop(stop_event, 15.0)

        self.assertEqual(stop_event.wait.call_args_list[0].args, (15.0,))
        self.assertEqual(stop_event.wait.call_count, 2)
        report.assert_called_once_with()

    def test_worker_ready_starts_dedicated_heartbeat_thread(self) -> None:
        settings = Mock(
            runner_worker_key="compose-worker-1",
            runner_pool_key="web",
            runner_heartbeat_interval_seconds=15.0,
        )
        thread = Mock()
        thread.is_alive.return_value = False

        with (
            patch.object(
                celery_application.WorkerSettings,
                "from_environment",
                return_value=settings,
            ),
            patch.object(celery_application, "Thread", return_value=thread) as thread_factory,
            patch.object(celery_application, "_runner_heartbeat_thread", None),
        ):
            celery_application._start_runner_heartbeat_thread()

        thread_factory.assert_called_once_with(
            target=celery_application._runner_heartbeat_loop,
            args=(celery_application._runner_heartbeat_stop, 15.0),
            name="testops-runner-heartbeat",
            daemon=True,
        )
        thread.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
