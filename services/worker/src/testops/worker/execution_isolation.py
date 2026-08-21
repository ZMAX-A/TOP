"""Parent-side lifecycle for credential-minimized Run subprocesses."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from testops.contracts import RunResult, RunSnapshot

from .isolated_executor import MAX_SNAPSHOT_BYTES

SAFE_PARENT_ENVIRONMENT = frozenset(
    {
        "PATH",
        "PATHEXT",
        "PYTHONPATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "LD_LIBRARY_PATH",
        "LANG",
        "LC_ALL",
        "PLAYWRIGHT_BROWSERS_PATH",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)


class IsolationSettings(Protocol):
    workspace_root: str
    runner_version: str
    runner_executor_poll_seconds: float
    runner_executor_timeout_seconds: int


class ExecutionControlPlane(Protocol):
    def cancel_requested(self, run_id: object) -> bool: ...

    def report_event(self, run_id: object, event: dict[str, object]) -> None: ...


class IsolatedExecutionError(RuntimeError):
    """The child process could not produce a valid immutable result."""


class IsolatedExecutionCanceled(IsolatedExecutionError):
    """The control plane requested cancellation while the child was running."""


class IsolatedExecutionTimedOut(IsolatedExecutionError):
    """The child exceeded its local executor timeout."""


def isolated_child_environment(
    job: RunSnapshot,
    *,
    sandbox_home: str | Path,
    executor_version: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if environ is None else environ
    environment = {
        name: value for name in SAFE_PARENT_ENVIRONMENT if (value := source.get(name)) is not None
    }
    if python_path := environment.get("PYTHONPATH"):
        parent_working_directory = Path.cwd()
        environment["PYTHONPATH"] = os.pathsep.join(
            str(
                candidate.resolve()
                if (candidate := Path(entry)).is_absolute()
                else (parent_working_directory / candidate).resolve()
            )
            for entry in python_path.split(os.pathsep)
            if entry
        )
    home = str(Path(sandbox_home).resolve())
    environment.update(
        {
            "HOME": home,
            "USERPROFILE": home,
            "TEMP": home,
            "TMP": home,
            "TMPDIR": home,
            "TESTOPS_EXECUTOR_MODE": "SUBPROCESS",
            "TESTOPS_EXECUTOR_VERSION": executor_version,
        }
    )
    for binding in job.secret_bindings:
        name = f"TESTOPS_SECRET_{binding.name}"
        value = source.get(name)
        if value:
            environment[name] = value
    return environment


def _read_result(workspace_root: str | Path, job: RunSnapshot) -> RunResult:
    root = Path(workspace_root).resolve()
    result_path = (root / str(job.run_id) / "run-result.json").resolve()
    if root not in result_path.parents or not result_path.is_file():
        raise IsolatedExecutionError("isolated executor did not produce an immutable result")
    try:
        result = RunResult.model_validate_json(result_path.read_text("utf-8"))
    except Exception as exc:
        raise IsolatedExecutionError("isolated executor produced an invalid result") from exc
    if result.run_id != job.run_id or result.execution_isolation is None:
        raise IsolatedExecutionError("isolated executor result evidence is inconsistent")
    return result


def forward_new_events(
    workspace_root: str | Path,
    job: RunSnapshot,
    control_plane: ExecutionControlPlane,
    offset: int,
) -> int:
    events_path = Path(workspace_root).resolve() / str(job.run_id) / "events.jsonl"
    if not events_path.is_file():
        return offset
    with events_path.open("rb") as stream:
        stream.seek(offset)
        payload = stream.read()
    complete_size = payload.rfind(b"\n") + 1
    if complete_size == 0:
        return offset
    for line in payload[:complete_size].splitlines():
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        try:
            control_plane.report_event(job.run_id, event)
        except Exception:
            # The parent task replays the immutable JSONL before the result callback.
            pass
    return offset + complete_size


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class SubprocessRunExecutor:
    def __init__(
        self,
        settings: IsolationSettings,
        control_plane: ExecutionControlPlane,
        *,
        environ: Mapping[str, str] | None = None,
    ):
        self._settings = settings
        self._control_plane = control_plane
        self._environ = os.environ if environ is None else environ

    def execute(self, job: RunSnapshot) -> RunResult:
        workspace_root = Path(self._settings.workspace_root).resolve()
        workspace_root.mkdir(parents=True, exist_ok=True)
        sandbox_parent = (workspace_root / ".executor").resolve()
        if workspace_root not in sandbox_parent.parents:
            raise IsolatedExecutionError("executor sandbox escaped the workspace root")
        sandbox_parent.mkdir(exist_ok=True)
        sandbox_home = Path(tempfile.mkdtemp(prefix=f"{job.run_id}-", dir=sandbox_parent)).resolve()
        payload = json.dumps(
            job.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_SNAPSHOT_BYTES:
            shutil.rmtree(sandbox_home)
            raise IsolatedExecutionError("Run Snapshot exceeds isolated executor input limit")
        environment = isolated_child_environment(
            job,
            sandbox_home=sandbox_home,
            executor_version=self._settings.runner_version,
            environ=self._environ,
        )
        command = (
            sys.executable,
            "-m",
            "testops.worker.isolated_executor",
            "--workspace-root",
            str(workspace_root),
        )
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                command,
                cwd=sandbox_home,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
            if process.stdin is None:
                raise IsolatedExecutionError("isolated executor stdin is unavailable")
            process.stdin.write(payload)
            process.stdin.close()
            deadline = time.monotonic() + self._settings.runner_executor_timeout_seconds
            event_offset = 0
            while process.poll() is None:
                event_offset = forward_new_events(
                    workspace_root,
                    job,
                    self._control_plane,
                    event_offset,
                )
                if self._control_plane.cancel_requested(job.run_id):
                    _stop_process(process)
                    raise IsolatedExecutionCanceled("isolated Run was canceled")
                if time.monotonic() >= deadline:
                    _stop_process(process)
                    raise IsolatedExecutionTimedOut("isolated Run exceeded executor timeout")
                time.sleep(self._settings.runner_executor_poll_seconds)
            forward_new_events(workspace_root, job, self._control_plane, event_offset)
            if process.returncode != 0:
                raise IsolatedExecutionError(
                    f"isolated executor exited without a result (code {process.returncode})"
                )
            return _read_result(workspace_root, job)
        finally:
            if process is not None:
                _stop_process(process)
            shutil.rmtree(sandbox_home, ignore_errors=True)
