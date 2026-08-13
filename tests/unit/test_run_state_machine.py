from __future__ import annotations

import unittest

from testops.api.state_machine import InvalidRunTransition, require_run_transition
from testops.contracts import RunStatus


class RunStateMachineTests(unittest.TestCase):
    def test_happy_path_and_terminal_transitions(self) -> None:
        require_run_transition(RunStatus.QUEUED, RunStatus.PREPARING)
        require_run_transition(RunStatus.PREPARING, RunStatus.RUNNING)
        require_run_transition(RunStatus.RUNNING, RunStatus.PASSED)

    def test_invalid_skip_and_terminal_rewrite_are_rejected(self) -> None:
        with self.assertRaisesRegex(InvalidRunTransition, "QUEUED -> PASSED"):
            require_run_transition(RunStatus.QUEUED, RunStatus.PASSED)
        with self.assertRaisesRegex(InvalidRunTransition, "PASSED -> RUNNING"):
            require_run_transition(RunStatus.PASSED, RunStatus.RUNNING)


if __name__ == "__main__":
    unittest.main()
