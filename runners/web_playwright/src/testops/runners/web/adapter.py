"""Adapter protocol implemented by the legacy YanJia Web automation package."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from testops.contracts import RunResult, RunSnapshot

ProgressReporter = Callable[[dict[str, object]], None]


class AutomationAdapter(ABC):
    @abstractmethod
    def validate(self, job: RunSnapshot) -> None:
        """Reject unsupported or incompatible jobs before allocating a browser."""

    @abstractmethod
    def prepare(self, job: RunSnapshot) -> None:
        """Prepare an isolated temporary directory and runtime resources."""

    @abstractmethod
    def execute(self, job: RunSnapshot, reporter: ProgressReporter) -> RunResult:
        """Execute the immutable job and return a structured terminal result."""

    @abstractmethod
    def cancel(self, run_id: str) -> None:
        """Request cancellation without mutating an already reported result."""

    @abstractmethod
    def collect(self, run_id: str) -> tuple[str, ...]:
        """Return collected artifact references for upload."""

    @abstractmethod
    def health(self) -> dict[str, object]:
        """Report adapter and browser availability."""
