"""Docker-backed hard isolation for one immutable Run Snapshot."""

from __future__ import annotations

import io
import json
import logging
import os
import secrets
import shutil
import string
import tarfile
import tempfile
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from testops.contracts import (
    LIVE_VIEW_RFB_PORT,
    LiveViewRegistration,
    RunExecutionIsolationEvidence,
    RunResult,
    RunSnapshot,
)

from .execution_isolation import (
    ExecutionControlPlane,
    IsolatedExecutionCanceled,
    IsolatedExecutionError,
    IsolatedExecutionTimedOut,
    _read_result,
)
from .isolated_executor import MAX_SNAPSHOT_BYTES, virtual_display_prefix
from .live_view_registry import LiveViewPublisher

CONTAINER_WORKSPACE_ROOT = "/var/lib/testops/runs"
CONTAINER_INPUT_ROOT = "/run/testops-input"
PACKAGE_DIGEST_LABEL = "io.testops.package.digest"
RUNTIME_UID_LABEL = "io.testops.runtime.uid"
RUNTIME_GID_LABEL = "io.testops.runtime.gid"
RUNTIME_UID = 1001
RUNTIME_GID = 1001
MAX_EVENT_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_RUN_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_RUN_ARCHIVE_FILES = 10_000
CORE_DUMP_ULIMIT = {"Name": "core", "Soft": 0, "Hard": 0}
LOGGER = logging.getLogger(__name__)


class ContainerIsolationSettings(Protocol):
    workspace_root: str
    runner_version: str
    runner_executor_poll_seconds: float
    runner_executor_timeout_seconds: int
    runner_container_image: str | None
    runner_container_network_policy: str
    runner_container_network: str | None
    runner_container_memory_bytes: int
    runner_container_cpu_millis: int
    runner_container_pids_limit: int
    runner_container_shm_bytes: int
    runner_live_view_enabled: bool
    runner_live_view_port: int
    runner_live_view_ready_timeout_seconds: float
    broker_url: str


def container_isolation_evidence(
    settings: ContainerIsolationSettings,
    runtime_image_id: str,
) -> RunExecutionIsolationEvidence:
    return RunExecutionIsolationEvidence(
        mode="CONTAINER",
        executor_version=settings.runner_version,
        dedicated_process=True,
        credential_scope="RUN_SECRETS_ONLY",
        read_only_root_filesystem=True,
        network_policy=settings.runner_container_network_policy,
        resource_limits_enforced=True,
        runtime_image_id=runtime_image_id,
        memory_limit_bytes=settings.runner_container_memory_bytes,
        cpu_limit_millis=settings.runner_container_cpu_millis,
        pids_limit=settings.runner_container_pids_limit,
    )


def container_environment(
    job: RunSnapshot,
    settings: ContainerIsolationSettings,
    runtime_image_id: str,
    *,
    environ: Mapping[str, str] | None = None,
    live_view_password: str | None = None,
) -> dict[str, str]:
    source = os.environ if environ is None else environ
    environment = {
        "TESTOPS_EXECUTOR_MODE": "CONTAINER",
        "TESTOPS_EXECUTOR_VERSION": settings.runner_version,
        "TESTOPS_RUNTIME_IMAGE_ID": runtime_image_id,
        "TESTOPS_NETWORK_POLICY": settings.runner_container_network_policy,
        "TESTOPS_MEMORY_LIMIT_BYTES": str(settings.runner_container_memory_bytes),
        "TESTOPS_CPU_LIMIT_MILLIS": str(settings.runner_container_cpu_millis),
        "TESTOPS_PIDS_LIMIT": str(settings.runner_container_pids_limit),
    }
    for binding in job.secret_bindings:
        name = f"TESTOPS_SECRET_{binding.name}"
        value = source.get(name)
        if value:
            environment[name] = value
    if live_view_password is not None:
        environment["TESTOPS_LIVE_VIEW_PASSWORD"] = live_view_password
    return environment


def _live_view_requested(job: RunSnapshot, settings: ContainerIsolationSettings) -> bool:
    return bool(
        settings.runner_live_view_enabled
        and job.web_config is not None
        and not job.web_config.headless
    )


def container_execution_command(
    job: RunSnapshot,
    *,
    live_view_enabled: bool = False,
    rfb_port: int = LIVE_VIEW_RFB_PORT,
) -> list[str]:
    command = [
        "python",
        "-m",
        "testops.worker.isolated_executor",
        "--workspace-root",
        CONTAINER_WORKSPACE_ROOT,
        "--input-file",
        f"{CONTAINER_INPUT_ROOT}/snapshot.json",
    ]
    if live_view_enabled:
        return [
            "python",
            "-m",
            "testops.worker.live_view_executor",
            "--display",
            ":99",
            "--width",
            "1440",
            "--height",
            "900",
            "--depth",
            "24",
            "--rfb-port",
            str(rfb_port),
            "--",
            *command,
        ]
    return [*virtual_display_prefix(job), *command]


def container_create_options(
    job: RunSnapshot,
    settings: ContainerIsolationSettings,
    runtime_image_id: str,
    volume_name: str,
    input_volume_name: str,
    *,
    environ: Mapping[str, str] | None = None,
    live_view_password: str | None = None,
) -> dict[str, object]:
    network = (
        settings.runner_container_network
        if settings.runner_container_network_policy == "ALLOWLIST"
        else "none"
    )
    if not network:
        raise IsolatedExecutionError("container allowlist network is unavailable")
    live_view_enabled = _live_view_requested(job, settings)
    if live_view_enabled and live_view_password is None:
        raise IsolatedExecutionError("headed live-view Run has no VNC password")
    return {
        "image": runtime_image_id,
        "command": container_execution_command(
            job,
            live_view_enabled=live_view_enabled,
            rfb_port=settings.runner_live_view_port,
        ),
        "name": f"testops-run-{job.run_id.hex}",
        "user": "pwuser",
        "working_dir": "/app",
        "environment": container_environment(
            job,
            settings,
            runtime_image_id,
            environ=environ,
            live_view_password=live_view_password,
        ),
        "labels": {
            "io.testops.managed": "true",
            "io.testops.run-id": str(job.run_id),
        },
        "volumes": {
            volume_name: {"bind": CONTAINER_WORKSPACE_ROOT, "mode": "rw"},
            input_volume_name: {"bind": CONTAINER_INPUT_ROOT, "mode": "ro"},
        },
        "tmpfs": {
            "/tmp": (
                f"rw,noexec,nosuid,nodev,size=256m,uid={RUNTIME_UID},gid={RUNTIME_GID},mode=0700"
            ),
            "/home/pwuser": (
                f"rw,noexec,nosuid,nodev,size=64m,uid={RUNTIME_UID},gid={RUNTIME_GID},mode=0700"
            ),
        },
        "network": network,
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "init": True,
        "mem_limit": settings.runner_container_memory_bytes,
        "nano_cpus": settings.runner_container_cpu_millis * 1_000_000,
        "pids_limit": settings.runner_container_pids_limit,
        "shm_size": settings.runner_container_shm_bytes,
        "ulimits": [CORE_DUMP_ULIMIT],
        "detach": True,
    }


def _snapshot_archive(job: RunSnapshot) -> bytes:
    payload = json.dumps(
        job.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise IsolatedExecutionError("Run Snapshot exceeds container executor input limit")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as stream:
        member = tarfile.TarInfo("snapshot.json")
        member.size = len(payload)
        member.mode = 0o444
        member.uid = RUNTIME_UID
        member.gid = RUNTIME_GID
        stream.addfile(member, io.BytesIO(payload))
    return archive.getvalue()


def _archive_file_bytes(
    chunks: Iterable[bytes],
    *,
    expected_name: str,
    maximum_bytes: int,
) -> bytes | None:
    archive = io.BytesIO()
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > maximum_bytes:
            raise IsolatedExecutionError("container event archive exceeded its size limit")
        archive.write(chunk)
    archive.seek(0)
    with tarfile.open(fileobj=archive, mode="r:*") as stream:
        for member in stream:
            if member.isfile() and PurePosixPath(member.name).name == expected_name:
                if member.size > maximum_bytes:
                    raise IsolatedExecutionError("container event file exceeded its size limit")
                source = stream.extractfile(member)
                if source is None:
                    return None
                return source.read(maximum_bytes + 1)
    return None


def _not_found(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) == 404:
        return True
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 404


def _container_event_bytes(container: Any, run_id: object) -> bytes | None:
    try:
        chunks, _ = container.get_archive(f"{CONTAINER_WORKSPACE_ROOT}/{run_id}/events.jsonl")
    except Exception as exc:
        if _not_found(exc):
            return None
        raise IsolatedExecutionError("container event stream is unavailable") from exc
    return _archive_file_bytes(
        chunks,
        expected_name="events.jsonl",
        maximum_bytes=MAX_EVENT_ARCHIVE_BYTES,
    )


def forward_container_events(
    container: Any,
    job: RunSnapshot,
    control_plane: ExecutionControlPlane,
    offset: int,
) -> int:
    payload = _container_event_bytes(container, job.run_id)
    if payload is None or len(payload) <= offset:
        return offset
    pending = payload[offset:]
    complete_size = pending.rfind(b"\n") + 1
    if complete_size == 0:
        return offset
    for line in pending[:complete_size].splitlines():
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        try:
            control_plane.report_event(job.run_id, event)
        except Exception:
            pass
    return offset + complete_size


def _safe_archive_path(member_name: str, run_id: str) -> Path | None:
    if member_name.startswith("/"):
        raise IsolatedExecutionError("container archive contains an absolute path")
    parts = [part for part in PurePosixPath(member_name).parts if part not in {"", "."}]
    if parts and parts[0] == run_id:
        parts = parts[1:]
    if not parts:
        return None
    if any(part == ".." for part in parts):
        raise IsolatedExecutionError("container archive escaped the Run directory")
    return Path(*parts)


def extract_run_archive(
    chunks: Iterable[bytes],
    *,
    workspace_root: str | Path,
    run_id: object,
) -> None:
    workspace = Path(workspace_root).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    destination = (workspace / str(run_id)).resolve()
    if workspace not in destination.parents or destination.exists():
        raise IsolatedExecutionError("container Run destination is not empty")
    temporary = Path(tempfile.mkdtemp(prefix=f".container-import-{run_id}-", dir=workspace))
    try:
        total_bytes = 0
        file_count = 0
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as archive:
            for chunk in chunks:
                total_bytes += len(chunk)
                if total_bytes > MAX_RUN_ARCHIVE_BYTES:
                    raise IsolatedExecutionError("container Run archive exceeded its size limit")
                archive.write(chunk)
            archive.seek(0)
            with tarfile.open(fileobj=archive, mode="r:*") as stream:
                for member in stream:
                    relative = _safe_archive_path(member.name, str(run_id))
                    if relative is None:
                        continue
                    target = (temporary / relative).resolve()
                    if temporary.resolve() not in target.parents:
                        raise IsolatedExecutionError("container archive escaped the import root")
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        raise IsolatedExecutionError("container archive contains a special file")
                    file_count += 1
                    if file_count > MAX_RUN_ARCHIVE_FILES:
                        raise IsolatedExecutionError("container Run archive has too many files")
                    source = stream.extractfile(member)
                    if source is None:
                        raise IsolatedExecutionError("container archive member is unreadable")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as output:
                        remaining = member.size
                        while remaining:
                            block = source.read(min(1024 * 1024, remaining))
                            if not block:
                                raise IsolatedExecutionError(
                                    "container archive member is truncated"
                                )
                            output.write(block)
                            remaining -= len(block)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def _docker_client() -> Any:
    try:
        import docker
    except ImportError as exc:
        raise IsolatedExecutionError("Docker SDK is unavailable to the container executor") from exc
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:
        raise IsolatedExecutionError(
            "Docker daemon is unavailable to the container executor"
        ) from exc
    return client


def _image_identity(client: Any, settings: ContainerIsolationSettings, job: RunSnapshot) -> str:
    if not settings.runner_container_image:
        raise IsolatedExecutionError("container executor image is not configured")
    try:
        image = client.images.get(settings.runner_container_image)
        image_id = str(image.id).lower()
        labels = image.attrs.get("Config", {}).get("Labels") or {}
    except Exception as exc:
        raise IsolatedExecutionError("container executor image is unavailable") from exc
    if not image_id.startswith("sha256:") or len(image_id) != 71:
        raise IsolatedExecutionError("container executor image has no immutable local ID")
    if str(labels.get(PACKAGE_DIGEST_LABEL, "")).lower() != job.automation_package.digest.lower():
        raise IsolatedExecutionError("container executor image does not match the admitted package")
    if labels.get(RUNTIME_UID_LABEL) != str(RUNTIME_UID) or labels.get(RUNTIME_GID_LABEL) != str(
        RUNTIME_GID
    ):
        raise IsolatedExecutionError("container executor image has an unexpected runtime identity")
    return image_id


def _terminate_container(container: Any) -> None:
    try:
        container.kill()
    except Exception as exc:
        if not _not_found(exc):
            raise IsolatedExecutionError("container executor could not terminate the Run") from exc


def _generate_live_view_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


def _wait_for_live_view(container: Any, *, port: int, timeout_seconds: float) -> None:
    probe = (
        "import socket; "
        f"connection=socket.create_connection(('127.0.0.1',{port}),timeout=0.25); "
        "connection.close()"
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        container.reload()
        if not container.attrs.get("State", {}).get("Running", False):
            raise RuntimeError("Run container exited before live view became ready")
        try:
            result = container.exec_run(["python", "-c", probe])
            exit_code_value = getattr(result, "exit_code", None)
            if exit_code_value is None:
                exit_code_value = result[0]
            exit_code = int(exit_code_value)
        except Exception:
            exit_code = -1
        if exit_code == 0:
            return
        time.sleep(0.2)
    raise RuntimeError("live-view VNC server did not become ready")


def _live_view_event(job: RunSnapshot, event: str) -> dict[str, object]:
    return {
        "run_id": str(job.run_id),
        "event": event,
        "at": datetime.now(UTC).isoformat(),
    }


def _managed_for_run(resource: Any, run_id: object) -> bool:
    labels = getattr(resource, "attrs", {}).get("Labels") or {}
    return labels.get("io.testops.managed") == "true" and labels.get("io.testops.run-id") == str(
        run_id
    )


def _reclaim_stale_resources(client: Any, job: RunSnapshot, resource_name: str) -> None:
    try:
        stale_container = client.containers.get(resource_name)
    except Exception as exc:
        if not _not_found(exc):
            raise IsolatedExecutionError(
                "container executor cannot inspect stale resources"
            ) from exc
    else:
        if not _managed_for_run(stale_container, job.run_id):
            raise IsolatedExecutionError("container executor resource name is already reserved")
        try:
            stale_container.remove(force=True)
        except Exception as exc:
            raise IsolatedExecutionError("container executor cannot reclaim a stale Run") from exc
    try:
        stale_volume = client.volumes.get(resource_name)
    except Exception as exc:
        if not _not_found(exc):
            raise IsolatedExecutionError("container executor cannot inspect stale volumes") from exc
    else:
        if not _managed_for_run(stale_volume, job.run_id):
            raise IsolatedExecutionError("container executor volume name is already reserved")
        try:
            stale_volume.remove(force=True)
        except Exception as exc:
            raise IsolatedExecutionError(
                "container executor cannot reclaim a stale volume"
            ) from exc


def _stage_snapshot(
    client: Any,
    job: RunSnapshot,
    runtime_image_id: str,
    input_volume_name: str,
) -> None:
    staging_container = None
    try:
        staging_container = client.containers.create(
            image=runtime_image_id,
            command=["python", "-c", "pass"],
            name=input_volume_name,
            user="pwuser",
            network="none",
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            labels={
                "io.testops.managed": "true",
                "io.testops.run-id": str(job.run_id),
                "io.testops.purpose": "snapshot-staging",
            },
            volumes={input_volume_name: {"bind": CONTAINER_INPUT_ROOT, "mode": "rw"}},
            detach=True,
        )
        if not staging_container.put_archive(CONTAINER_INPUT_ROOT, _snapshot_archive(job)):
            raise IsolatedExecutionError("container executor rejected the Run Snapshot")
    finally:
        if staging_container is not None:
            try:
                staging_container.remove(force=True)
            except Exception:
                pass


def verify_container_isolation(
    container: Any,
    settings: ContainerIsolationSettings,
    runtime_image_id: str,
) -> None:
    container.reload()
    attributes = container.attrs
    host = attributes.get("HostConfig", {})
    config = attributes.get("Config", {})
    expected_network = (
        settings.runner_container_network
        if settings.runner_container_network_policy == "ALLOWLIST"
        else "none"
    )
    mismatches: list[str] = []
    exact_values = {
        "Image": (attributes.get("Image"), runtime_image_id),
        "User": (config.get("User"), "pwuser"),
        "ReadonlyRootfs": (host.get("ReadonlyRootfs"), True),
        "Privileged": (host.get("Privileged"), False),
        "NetworkMode": (host.get("NetworkMode"), expected_network),
        "Memory": (host.get("Memory"), settings.runner_container_memory_bytes),
        "NanoCpus": (
            host.get("NanoCpus"),
            settings.runner_container_cpu_millis * 1_000_000,
        ),
        "PidsLimit": (host.get("PidsLimit"), settings.runner_container_pids_limit),
        "ShmSize": (host.get("ShmSize"), settings.runner_container_shm_bytes),
    }
    mismatches.extend(
        field for field, (actual, expected) in exact_values.items() if actual != expected
    )
    if "ALL" not in {str(value).upper() for value in (host.get("CapDrop") or [])}:
        mismatches.append("CapDrop")
    if "no-new-privileges:true" not in (host.get("SecurityOpt") or []):
        mismatches.append("SecurityOpt")
    core_ulimit = next(
        (
            value
            for value in (host.get("Ulimits") or [])
            if isinstance(value, dict) and value.get("Name") == "core"
        ),
        None,
    )
    if core_ulimit != CORE_DUMP_ULIMIT:
        mismatches.append("CoreUlimit")
    tmpfs = host.get("Tmpfs") or {}
    if not {"/tmp", "/home/pwuser"}.issubset(tmpfs):
        mismatches.append("Tmpfs")
    mounts = {
        str(mount.get("Destination")): mount
        for mount in attributes.get("Mounts", [])
        if isinstance(mount, dict)
    }
    workspace_mount = mounts.get(CONTAINER_WORKSPACE_ROOT, {})
    input_mount = mounts.get(CONTAINER_INPUT_ROOT, {})
    if workspace_mount.get("Type") != "volume" or workspace_mount.get("RW") is not True:
        mismatches.append("WorkspaceMount")
    if input_mount.get("Type") != "volume" or input_mount.get("RW") is not False:
        mismatches.append("InputMount")
    if "/var/run/docker.sock" in mounts:
        mismatches.append("DockerSocket")
    if mismatches:
        raise IsolatedExecutionError(
            "container runtime does not match declared isolation: " + ", ".join(sorted(mismatches))
        )


class ContainerRunExecutor:
    def __init__(
        self,
        settings: ContainerIsolationSettings,
        control_plane: ExecutionControlPlane,
        *,
        environ: Mapping[str, str] | None = None,
        client: Any | None = None,
        live_view_publisher: LiveViewPublisher | None = None,
    ):
        self._settings = settings
        self._control_plane = control_plane
        self._environ = os.environ if environ is None else environ
        self._client = client
        self._live_view_publisher = live_view_publisher

    def execute(self, job: RunSnapshot) -> RunResult:
        phase = "daemon"
        client = self._client or _docker_client()
        phase = "image-admission"
        image_id = _image_identity(client, self._settings, job)
        expected_evidence = container_isolation_evidence(self._settings, image_id)
        volume_name = f"testops-run-{job.run_id.hex}"
        input_volume_name = f"{volume_name}-input"
        container = None
        volume = None
        input_volume = None
        started = False
        isolation_verified = False
        live_view_enabled = _live_view_requested(job, self._settings)
        live_view_password = _generate_live_view_password() if live_view_enabled else None
        live_view_registration: LiveViewRegistration | None = None
        live_view_document: str | None = None
        live_view_publisher = self._live_view_publisher
        owns_live_view_publisher = False
        try:
            phase = "stale-resource-recovery"
            _reclaim_stale_resources(client, job, volume_name)
            _reclaim_stale_resources(client, job, input_volume_name)
            phase = "workspace-volume"
            volume = client.volumes.create(
                name=volume_name,
                labels={"io.testops.managed": "true", "io.testops.run-id": str(job.run_id)},
            )
            input_volume = client.volumes.create(
                name=input_volume_name,
                labels={
                    "io.testops.managed": "true",
                    "io.testops.run-id": str(job.run_id),
                    "io.testops.purpose": "snapshot-input",
                },
            )
            phase = "snapshot-import"
            _stage_snapshot(client, job, image_id, input_volume_name)
            options = container_create_options(
                job,
                self._settings,
                image_id,
                volume_name,
                input_volume_name,
                environ=self._environ,
                live_view_password=live_view_password,
            )
            phase = "container-create"
            container = client.containers.create(**options)
            phase = "container-start"
            container.start()
            started = True
            phase = "isolation-verification"
            verify_container_isolation(container, self._settings, image_id)
            isolation_verified = True
            if live_view_enabled and live_view_password is not None:
                try:
                    phase = "live-view-readiness"
                    _wait_for_live_view(
                        container,
                        port=self._settings.runner_live_view_port,
                        timeout_seconds=self._settings.runner_live_view_ready_timeout_seconds,
                    )
                    if live_view_publisher is None:
                        live_view_publisher = LiveViewPublisher(self._settings.broker_url)
                        owns_live_view_publisher = True
                    live_view_registration = LiveViewRegistration(
                        run_id=job.run_id,
                        session_id=secrets.token_urlsafe(32),
                        host=volume_name,
                        port=self._settings.runner_live_view_port,
                        password=live_view_password,
                    )
                    live_view_document = live_view_publisher.publish(
                        live_view_registration,
                        ttl_seconds=self._settings.runner_executor_timeout_seconds + 60,
                    )
                    try:
                        self._control_plane.report_event(
                            job.run_id,
                            _live_view_event(job, "live_view_ready"),
                        )
                    except Exception:
                        pass
                except Exception as exc:
                    LOGGER.warning("Run %s live view is unavailable: %s", job.run_id, exc)
            deadline = time.monotonic() + self._settings.runner_executor_timeout_seconds
            event_offset = 0
            while True:
                phase = "container-poll"
                container.reload()
                state = container.attrs.get("State", {})
                if not state.get("Running", False):
                    break
                event_offset = forward_container_events(
                    container,
                    job,
                    self._control_plane,
                    event_offset,
                )
                if self._control_plane.cancel_requested(job.run_id):
                    _terminate_container(container)
                    raise IsolatedExecutionCanceled("isolated Run was canceled")
                if time.monotonic() >= deadline:
                    _terminate_container(container)
                    raise IsolatedExecutionTimedOut("isolated Run exceeded executor timeout")
                time.sleep(self._settings.runner_executor_poll_seconds)
            forward_container_events(container, job, self._control_plane, event_offset)
            exit_code = int(container.attrs.get("State", {}).get("ExitCode", -1))
            if exit_code != 0:
                raise IsolatedExecutionError(
                    f"container executor exited without a result (code {exit_code})"
                )
            phase = "result-export"
            chunks, _ = container.get_archive(f"{CONTAINER_WORKSPACE_ROOT}/{job.run_id}")
            extract_run_archive(
                chunks,
                workspace_root=self._settings.workspace_root,
                run_id=job.run_id,
            )
            result = _read_result(self._settings.workspace_root, job)
            if result.execution_isolation != expected_evidence:
                raise IsolatedExecutionError(
                    "container executor isolation evidence is inconsistent"
                )
            return result
        except (
            IsolatedExecutionCanceled,
            IsolatedExecutionTimedOut,
            IsolatedExecutionError,
        ) as exc:
            if started and isolation_verified:
                exc.execution_isolation = expected_evidence
            raise
        except Exception as exc:
            raise IsolatedExecutionError(
                f"container executor failed closed during {phase}"
            ) from exc
        finally:
            if (
                live_view_publisher is not None
                and live_view_registration is not None
                and live_view_document is not None
            ):
                try:
                    live_view_publisher.unpublish(live_view_registration, live_view_document)
                except Exception:
                    pass
                try:
                    self._control_plane.report_event(
                        job.run_id,
                        _live_view_event(job, "live_view_ended"),
                    )
                except Exception:
                    pass
            if owns_live_view_publisher and live_view_publisher is not None:
                try:
                    live_view_publisher.close()
                except Exception:
                    pass
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass
            if volume is not None:
                try:
                    volume.remove(force=True)
                except Exception:
                    pass
            if input_volume is not None:
                try:
                    input_volume.remove(force=True)
                except Exception:
                    pass
