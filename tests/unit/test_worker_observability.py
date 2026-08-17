from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from testops.contracts import RunSnapshot
from testops.worker.tasks import _reconcile_events, execute_run

ROOT = Path(__file__).resolve().parents[2]


class RecordingControlPlane:
    def __init__(self) -> None:
        self.events: list[tuple[UUID, dict[str, object]]] = []

    def report_event(self, run_id: UUID, event: dict[str, object]) -> None:
        self.events.append((run_id, event))


class WorkerObservabilityTests(unittest.TestCase):
    def test_terminal_run_is_ignored_before_local_result_recovery(self) -> None:
        job = RunSnapshot.model_validate_json(
            (ROOT / "tests/fixtures/run_snapshot.valid.json").read_text("utf-8")
        )
        control_plane = Mock()
        control_plane.run_state.return_value = {
            "status": "TIMED_OUT",
            "cancel_requested": True,
        }
        settings = SimpleNamespace(
            control_plane_url="http://control-plane.invalid",
            runner_callback_token="runner-token",
        )
        with (
            patch(
                "testops.worker.tasks.WorkerSettings.from_environment",
                return_value=settings,
            ),
            patch("testops.worker.tasks.ControlPlaneClient", return_value=control_plane),
            patch(
                "testops.worker.tasks._existing_result",
                side_effect=AssertionError("terminal Run must not read local result"),
            ),
        ):
            result = execute_run.run(job.model_dump(mode="json", exclude_none=True))

        self.assertEqual(result["status"], "TIMED_OUT")
        self.assertTrue(result["ignored"])

    def test_local_events_are_replayed_in_file_order(self) -> None:
        run_id = uuid4()
        documents = [
            {"event": "run_started", "run_id": str(run_id), "at": "2026-08-12T08:00:00Z"},
            {
                "event": "case_started",
                "run_id": str(run_id),
                "case_code": "TC-LOGIN-001",
                "at": "2026-08-12T08:00:01Z",
            },
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = Path(temporary_directory) / str(run_id)
            run_directory.mkdir()
            (run_directory / "events.jsonl").write_text(
                "".join(json.dumps(document) + "\n" for document in documents),
                encoding="utf-8",
            )
            client = RecordingControlPlane()

            _reconcile_events(temporary_directory, run_id, client)  # type: ignore[arg-type]

        self.assertEqual([event for _run_id, event in client.events], documents)
        self.assertTrue(all(recorded_run_id == run_id for recorded_run_id, _ in client.events))

    def test_invalid_local_event_stops_result_delivery(self) -> None:
        run_id = uuid4()
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = Path(temporary_directory) / str(run_id)
            run_directory.mkdir()
            (run_directory / "events.jsonl").write_text("not-json\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "line 1"):
                _reconcile_events(  # type: ignore[arg-type]
                    temporary_directory,
                    run_id,
                    RecordingControlPlane(),
                )


if __name__ == "__main__":
    unittest.main()
