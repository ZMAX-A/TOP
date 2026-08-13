"""Read and validate immutable Runner jobs."""

from __future__ import annotations

import json
from pathlib import Path

from testops.contracts import RunSnapshot


def load_run_snapshot(path: str | Path) -> RunSnapshot:
    job_path = Path(path)
    if not job_path.is_file():
        raise FileNotFoundError(f"run snapshot not found: {job_path}")
    payload = json.loads(job_path.read_text(encoding="utf-8"))
    return RunSnapshot.model_validate(payload)
