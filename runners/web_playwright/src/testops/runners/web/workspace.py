"""Isolated per-run workspace and local artifact metadata."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid5

from testops.contracts import Artifact, ArtifactKind, RunResult


class WorkspaceError(RuntimeError):
    """A run workspace cannot be safely created or reused."""


class RunWorkspace:
    def __init__(self, root: str | Path, run_id: UUID):
        root_path = Path(root).resolve()
        root_path.mkdir(parents=True, exist_ok=True)
        run_path = (root_path / str(run_id)).resolve()
        if root_path not in run_path.parents:
            raise WorkspaceError("resolved run workspace escaped its configured root")
        if run_path.exists():
            raise WorkspaceError(f"run workspace already exists: {run_id}")
        run_path.mkdir()
        self.root = root_path
        self.path = run_path
        self.run_id = run_id
        self.events_path = run_path / "events.jsonl"
        (run_path / "screenshots").mkdir()
        (run_path / "traces").mkdir()

    def append_event(self, event: dict[str, object]) -> None:
        line = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.events_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")

    def artifact_path(self, directory: str, filename: str) -> Path:
        candidate = (self.path / directory / filename).resolve()
        if self.path not in candidate.parents:
            raise WorkspaceError("artifact path escaped run workspace")
        return candidate

    def artifacts(self) -> tuple[Artifact, ...]:
        artifacts: list[Artifact] = []
        for path in sorted(item for item in self.path.rglob("*") if item.is_file()):
            if path.name == "run-result.json":
                continue
            relative = path.relative_to(self.path).as_posix()
            payload = path.read_bytes()
            suffix = path.suffix.lower()
            if suffix == ".png":
                kind = ArtifactKind.SCREENSHOT
            elif suffix == ".zip":
                kind = ArtifactKind.TRACE
            elif suffix == ".webm":
                kind = ArtifactKind.VIDEO
            elif suffix in {".log", ".jsonl"}:
                kind = ArtifactKind.LOG
            else:
                kind = ArtifactKind.OTHER
            artifacts.append(
                Artifact(
                    artifact_id=uuid5(self.run_id, relative),
                    kind=kind,
                    name=path.name,
                    uri=f"workspace://{self.run_id}/{relative}",
                    digest=f"sha256:{hashlib.sha256(payload).hexdigest()}",
                    size_bytes=len(payload),
                )
            )
        return tuple(artifacts)

    def write_result(self, result: RunResult) -> Path:
        destination = self.path / "run-result.json"
        if destination.exists():
            raise WorkspaceError("run result already exists")
        destination.write_text(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return destination
