"""Explicit state transitions for persisted Test Runs."""

from __future__ import annotations

from testops.contracts import RunStatus

ALLOWED_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.PREPARING, RunStatus.CANCELED, RunStatus.INFRA_ERROR}),
    RunStatus.PREPARING: frozenset({RunStatus.RUNNING, RunStatus.CANCELED, RunStatus.INFRA_ERROR}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.PASSED,
            RunStatus.FAILED,
            RunStatus.CANCELED,
            RunStatus.TIMED_OUT,
            RunStatus.INFRA_ERROR,
        }
    ),
    RunStatus.PASSED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELED: frozenset(),
    RunStatus.TIMED_OUT: frozenset(),
    RunStatus.INFRA_ERROR: frozenset(),
}


class InvalidRunTransition(ValueError):
    pass


def require_run_transition(current: RunStatus, target: RunStatus) -> None:
    if target not in ALLOWED_RUN_TRANSITIONS[current]:
        raise InvalidRunTransition(f"invalid Run transition: {current.value} -> {target.value}")
