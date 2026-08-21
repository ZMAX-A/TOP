"""Kubernetes Job-backed hard isolation for one immutable Run Snapshot."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shlex
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from testops.contracts import RunExecutionIsolationEvidence, RunResult, RunSnapshot

from .container_execution import (
    MAX_EVENT_ARCHIVE_BYTES,
    MAX_RUN_ARCHIVE_BYTES,
    extract_run_archive,
)
from .execution_isolation import (
    ExecutionControlPlane,
    IsolatedExecutionCanceled,
    IsolatedExecutionError,
    IsolatedExecutionTimedOut,
    _read_result,
)
from .isolated_executor import MAX_SNAPSHOT_BYTES

KUBERNETES_WORKSPACE_ROOT = "/var/lib/testops/runs"
KUBERNETES_INPUT_ROOT = "/run/testops-input"
KUBERNETES_RUNTIME_UID = 1001
KUBERNETES_RUNTIME_GID = 1001
MAX_CONFIG_MAP_SNAPSHOT_BYTES = 768 * 1024
IMAGE_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
FINISHED_COMMAND = (
    "if [ -f /tmp/testops-finished ]; then cat /tmp/testops-exit-code; else printf pending; fi"
)


class KubernetesIsolationSettings(Protocol):
    workspace_root: str
    runner_version: str
    runner_executor_poll_seconds: float
    runner_executor_timeout_seconds: int
    runner_kubernetes_namespace: str
    runner_kubernetes_service_account: str
    runner_kubernetes_network_policy: str
    runner_kubernetes_network_policy_enforced: bool
    runner_kubernetes_allow_cidrs: tuple[str, ...]
    runner_kubernetes_memory_bytes: int
    runner_kubernetes_cpu_millis: int
    runner_kubernetes_ephemeral_storage_bytes: int
    runner_kubernetes_cleanup_ttl_seconds: int
    runner_kubernetes_in_cluster: bool
    runner_kubernetes_context: str | None


@dataclass(frozen=True, slots=True)
class KubernetesClientBundle:
    batch: Any
    core: Any
    networking: Any
    stream: Any


def kubernetes_isolation_evidence(
    settings: KubernetesIsolationSettings,
    runtime_image_id: str,
) -> RunExecutionIsolationEvidence:
    return RunExecutionIsolationEvidence(
        mode="KUBERNETES",
        executor_version=settings.runner_version,
        dedicated_process=True,
        credential_scope="RUN_SECRETS_ONLY",
        read_only_root_filesystem=True,
        network_policy=settings.runner_kubernetes_network_policy,
        resource_limits_enforced=True,
        runtime_image_id=runtime_image_id,
        memory_limit_bytes=settings.runner_kubernetes_memory_bytes,
        cpu_limit_millis=settings.runner_kubernetes_cpu_millis,
        ephemeral_storage_limit_bytes=settings.runner_kubernetes_ephemeral_storage_bytes,
        orchestrator_namespace=settings.runner_kubernetes_namespace,
        service_account_name=settings.runner_kubernetes_service_account,
        service_account_token_automounted=False,
    )


def _runtime_image(job: RunSnapshot) -> str:
    return f"{job.automation_package.image_repository}@{job.automation_package.digest}"


def _snapshot_text(job: RunSnapshot) -> str:
    payload = json.dumps(
        job.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    size = len(payload.encode("utf-8"))
    if size > MAX_SNAPSHOT_BYTES or size > MAX_CONFIG_MAP_SNAPSHOT_BYTES:
        raise IsolatedExecutionError("Run Snapshot exceeds Kubernetes ConfigMap input limit")
    return payload


def _resource_names(job: RunSnapshot) -> dict[str, str]:
    base = f"testops-run-{job.run_id.hex}"
    return {
        "job": base,
        "input": f"{base}-input",
        "secret": f"{base}-secrets",
        "network_policy": f"{base}-egress",
    }


def _labels(job: RunSnapshot) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": "testops-runner",
        "app.kubernetes.io/managed-by": "testops-worker",
        "io.testops.managed": "true",
        "io.testops.run-id": str(job.run_id),
    }


def _fixed_environment(
    job: RunSnapshot,
    settings: KubernetesIsolationSettings,
) -> list[dict[str, object]]:
    values = {
        "TESTOPS_EXECUTOR_MODE": "KUBERNETES",
        "TESTOPS_EXECUTOR_VERSION": settings.runner_version,
        "TESTOPS_RUNTIME_IMAGE_ID": job.automation_package.digest,
        "TESTOPS_NETWORK_POLICY": settings.runner_kubernetes_network_policy,
        "TESTOPS_MEMORY_LIMIT_BYTES": str(settings.runner_kubernetes_memory_bytes),
        "TESTOPS_CPU_LIMIT_MILLIS": str(settings.runner_kubernetes_cpu_millis),
        "TESTOPS_EPHEMERAL_STORAGE_LIMIT_BYTES": str(
            settings.runner_kubernetes_ephemeral_storage_bytes
        ),
        "TESTOPS_ORCHESTRATOR_NAMESPACE": settings.runner_kubernetes_namespace,
        "TESTOPS_SERVICE_ACCOUNT_NAME": settings.runner_kubernetes_service_account,
        "TESTOPS_SERVICE_ACCOUNT_TOKEN_AUTOMOUNTED": "false",
    }
    return [{"name": name, "value": value} for name, value in values.items()]


def kubernetes_resources(
    job: RunSnapshot,
    settings: KubernetesIsolationSettings,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, dict[str, object] | None]:
    if not settings.runner_kubernetes_network_policy_enforced:
        raise IsolatedExecutionError("Kubernetes NetworkPolicy enforcement has not been attested")
    source = os.environ if environ is None else environ
    names = _resource_names(job)
    labels = _labels(job)
    secret_values: dict[str, str] = {}
    secret_environment: list[dict[str, object]] = []
    for binding in job.secret_bindings:
        environment_name = f"TESTOPS_SECRET_{binding.name}"
        value = source.get(environment_name)
        if not value:
            continue
        secret_values[environment_name] = value
        secret_environment.append(
            {
                "name": environment_name,
                "valueFrom": {
                    "secretKeyRef": {
                        "name": names["secret"],
                        "key": environment_name,
                        "optional": False,
                    }
                },
            }
        )

    command = " ".join(
        (
            "set +e;",
            "python -m testops.worker.isolated_executor",
            f"--workspace-root {shlex.quote(KUBERNETES_WORKSPACE_ROOT)}",
            f"--input-file {shlex.quote(KUBERNETES_INPUT_ROOT + '/snapshot.json')};",
            "code=$?;",
            "printf '%s' \"$code\" > /tmp/testops-exit-code;",
            "touch /tmp/testops-finished;",
            "while :; do sleep 30; done",
        )
    )
    resources = {
        "requests": {
            "memory": str(settings.runner_kubernetes_memory_bytes),
            "cpu": f"{settings.runner_kubernetes_cpu_millis}m",
            "ephemeral-storage": str(settings.runner_kubernetes_ephemeral_storage_bytes),
        },
        "limits": {
            "memory": str(settings.runner_kubernetes_memory_bytes),
            "cpu": f"{settings.runner_kubernetes_cpu_millis}m",
            "ephemeral-storage": str(settings.runner_kubernetes_ephemeral_storage_bytes),
        },
    }
    container = {
        "name": "runner",
        "image": _runtime_image(job),
        "imagePullPolicy": "IfNotPresent",
        "command": ["/bin/sh", "-c", command],
        "workingDir": "/app",
        "env": [*_fixed_environment(job, settings), *secret_environment],
        "resources": resources,
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "runAsNonRoot": True,
            "runAsUser": KUBERNETES_RUNTIME_UID,
            "runAsGroup": KUBERNETES_RUNTIME_GID,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "volumeMounts": [
            {"name": "workspace", "mountPath": KUBERNETES_WORKSPACE_ROOT},
            {"name": "input", "mountPath": KUBERNETES_INPUT_ROOT, "readOnly": True},
            {"name": "tmp", "mountPath": "/tmp"},
            {"name": "home", "mountPath": "/home/pwuser"},
        ],
    }
    pod_spec = {
        "automountServiceAccountToken": False,
        "serviceAccountName": settings.runner_kubernetes_service_account,
        "restartPolicy": "Never",
        "enableServiceLinks": False,
        "hostNetwork": False,
        "hostPID": False,
        "hostIPC": False,
        "shareProcessNamespace": False,
        "terminationGracePeriodSeconds": 5,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": KUBERNETES_RUNTIME_UID,
            "runAsGroup": KUBERNETES_RUNTIME_GID,
            "fsGroup": KUBERNETES_RUNTIME_GID,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [container],
        "volumes": [
            {
                "name": "workspace",
                "emptyDir": {"sizeLimit": str(settings.runner_kubernetes_ephemeral_storage_bytes)},
            },
            {
                "name": "input",
                "configMap": {"name": names["input"], "defaultMode": 0o444},
            },
            {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "268435456"}},
            {"name": "home", "emptyDir": {"medium": "Memory", "sizeLimit": "67108864"}},
        ],
    }
    job_resource: dict[str, object] = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": names["job"],
            "namespace": settings.runner_kubernetes_namespace,
            "labels": labels,
            "annotations": {"io.testops.package.digest": job.automation_package.digest},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": settings.runner_executor_timeout_seconds + 60,
            "ttlSecondsAfterFinished": settings.runner_kubernetes_cleanup_ttl_seconds,
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }
    egress: list[dict[str, object]] = []
    if settings.runner_kubernetes_network_policy == "ALLOWLIST":
        egress.append(
            {"to": [{"ipBlock": {"cidr": cidr}} for cidr in settings.runner_kubernetes_allow_cidrs]}
        )
        egress.append(
            {
                "to": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                        },
                        "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                    }
                ],
                "ports": [
                    {"protocol": "UDP", "port": 53},
                    {"protocol": "TCP", "port": 53},
                ],
            }
        )
    return {
        "config_map": {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "immutable": True,
            "metadata": {
                "name": names["input"],
                "namespace": settings.runner_kubernetes_namespace,
                "labels": labels,
            },
            "data": {"snapshot.json": _snapshot_text(job)},
        },
        "secret": (
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "immutable": True,
                "type": "Opaque",
                "metadata": {
                    "name": names["secret"],
                    "namespace": settings.runner_kubernetes_namespace,
                    "labels": labels,
                },
                "stringData": secret_values,
            }
            if secret_values
            else None
        ),
        "network_policy": {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": names["network_policy"],
                "namespace": settings.runner_kubernetes_namespace,
                "labels": labels,
            },
            "spec": {
                "podSelector": {"matchLabels": {"io.testops.run-id": str(job.run_id)}},
                "policyTypes": ["Egress"],
                "egress": egress,
            },
        },
        "job": job_resource,
    }


def _kubernetes_clients(settings: KubernetesIsolationSettings) -> KubernetesClientBundle:
    try:
        from kubernetes import client, config
        from kubernetes.stream import stream
    except ImportError as exc:
        raise IsolatedExecutionError("Kubernetes SDK is unavailable to the Job executor") from exc
    try:
        if settings.runner_kubernetes_in_cluster:
            config.load_incluster_config()
        else:
            config.load_kube_config(context=settings.runner_kubernetes_context)
    except Exception as exc:
        raise IsolatedExecutionError("Kubernetes client configuration is unavailable") from exc
    return KubernetesClientBundle(
        batch=client.BatchV1Api(),
        core=client.CoreV1Api(),
        networking=client.NetworkingV1Api(),
        stream=stream,
    )


def _as_mapping(resource: Any) -> Mapping[str, Any]:
    if isinstance(resource, Mapping):
        return resource
    if hasattr(resource, "to_dict"):
        converted = resource.to_dict()
        if isinstance(converted, Mapping):
            return converted
    raise IsolatedExecutionError("Kubernetes API returned an unreadable resource")


def _value(document: Mapping[str, Any], camel: str, snake: str | None = None) -> Any:
    if camel in document:
        return document[camel]
    return document.get(snake or camel)


def _nested(document: Mapping[str, Any], camel: str, snake: str | None = None) -> Mapping[str, Any]:
    value = _value(document, camel, snake)
    return value if isinstance(value, Mapping) else {}


def _sequence(document: Mapping[str, Any], camel: str, snake: str | None = None) -> Sequence[Any]:
    value = _value(document, camel, snake)
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def _image_digest(value: object) -> str | None:
    match = IMAGE_DIGEST_PATTERN.search(str(value).lower())
    return match.group(0) if match else None


def verify_kubernetes_isolation(
    job_resource: Any,
    pod_resource: Any,
    network_policy_resource: Any,
    settings: KubernetesIsolationSettings,
    expected_image: str,
) -> str:
    job_document = _as_mapping(job_resource)
    pod_document = _as_mapping(pod_resource)
    policy_document = _as_mapping(network_policy_resource)
    job_spec = _nested(job_document, "spec")
    template = _nested(job_spec, "template")
    declared_pod_spec = _nested(template, "spec")
    actual_pod_spec = _nested(pod_document, "spec")
    actual_status = _nested(pod_document, "status")
    mismatches: list[str] = []
    if _value(job_spec, "backoffLimit", "backoff_limit") != 0:
        mismatches.append("BackoffLimit")
    if _value(job_spec, "activeDeadlineSeconds", "active_deadline_seconds") != (
        settings.runner_executor_timeout_seconds + 60
    ):
        mismatches.append("ActiveDeadline")
    if _value(job_spec, "ttlSecondsAfterFinished", "ttl_seconds_after_finished") != (
        settings.runner_kubernetes_cleanup_ttl_seconds
    ):
        mismatches.append("CleanupTTL")
    for candidate in (declared_pod_spec, actual_pod_spec):
        if (
            _value(candidate, "automountServiceAccountToken", "automount_service_account_token")
            is not False
        ):
            mismatches.append("ServiceAccountToken")
        if (
            _value(candidate, "serviceAccountName", "service_account_name")
            != settings.runner_kubernetes_service_account
        ):
            mismatches.append("ServiceAccount")
        for field, snake in (
            ("hostNetwork", "host_network"),
            ("hostPID", "host_pid"),
            ("hostIPC", "host_ipc"),
        ):
            if _value(candidate, field, snake) is not False:
                mismatches.append(field)
        pod_security = _nested(candidate, "securityContext", "security_context")
        if _value(pod_security, "runAsNonRoot", "run_as_non_root") is not True:
            mismatches.append("PodRunAsNonRoot")
        if _value(pod_security, "runAsUser", "run_as_user") != KUBERNETES_RUNTIME_UID:
            mismatches.append("PodRunAsUser")
        if _value(pod_security, "runAsGroup", "run_as_group") != KUBERNETES_RUNTIME_GID:
            mismatches.append("PodRunAsGroup")
        if _value(pod_security, "fsGroup", "fs_group") != KUBERNETES_RUNTIME_GID:
            mismatches.append("PodFsGroup")
        pod_seccomp = _nested(pod_security, "seccompProfile", "seccomp_profile")
        if _value(pod_seccomp, "type") != "RuntimeDefault":
            mismatches.append("PodSeccomp")
        containers = _sequence(candidate, "containers")
        runner = _as_mapping(containers[0]) if len(containers) == 1 else {}
        if runner.get("image") != expected_image:
            mismatches.append("Image")
        security = _nested(runner, "securityContext", "security_context")
        if _value(security, "readOnlyRootFilesystem", "read_only_root_filesystem") is not True:
            mismatches.append("ReadOnlyRootFilesystem")
        if _value(security, "allowPrivilegeEscalation", "allow_privilege_escalation") is not False:
            mismatches.append("PrivilegeEscalation")
        if _value(security, "runAsNonRoot", "run_as_non_root") is not True:
            mismatches.append("RunAsNonRoot")
        if _value(security, "runAsUser", "run_as_user") != KUBERNETES_RUNTIME_UID:
            mismatches.append("RunAsUser")
        if _value(security, "runAsGroup", "run_as_group") != KUBERNETES_RUNTIME_GID:
            mismatches.append("RunAsGroup")
        capabilities = _nested(security, "capabilities")
        if list(_sequence(capabilities, "drop")) != ["ALL"]:
            mismatches.append("Capabilities")
        seccomp = _nested(security, "seccompProfile", "seccomp_profile")
        if _value(seccomp, "type") != "RuntimeDefault":
            mismatches.append("Seccomp")
        resources = _nested(runner, "resources")
        expected_resources = {
            "memory": str(settings.runner_kubernetes_memory_bytes),
            "cpu": f"{settings.runner_kubernetes_cpu_millis}m",
            "ephemeral-storage": str(settings.runner_kubernetes_ephemeral_storage_bytes),
        }
        if _nested(resources, "limits") != expected_resources:
            mismatches.append("ResourceLimits")
        if _nested(resources, "requests") != expected_resources:
            mismatches.append("ResourceRequests")
        mounts = {
            str(_value(_as_mapping(item), "mountPath", "mount_path")): _as_mapping(item)
            for item in _sequence(runner, "volumeMounts", "volume_mounts")
        }
        if KUBERNETES_WORKSPACE_ROOT not in mounts:
            mismatches.append("WorkspaceMount")
        input_mount = mounts.get(KUBERNETES_INPUT_ROOT, {})
        if _value(input_mount, "readOnly", "read_only") is not True:
            mismatches.append("InputMount")
        if "/var/run/secrets/kubernetes.io/serviceaccount" in mounts:
            mismatches.append("ServiceAccountTokenMount")
        volumes = {
            str(_value(_as_mapping(item), "name")): _as_mapping(item)
            for item in _sequence(candidate, "volumes")
        }
        if set(volumes) != {"workspace", "input", "tmp", "home"}:
            mismatches.append("Volumes")
        workspace_volume = _nested(volumes.get("workspace", {}), "emptyDir", "empty_dir")
        if _value(workspace_volume, "sizeLimit", "size_limit") != str(
            settings.runner_kubernetes_ephemeral_storage_bytes
        ):
            mismatches.append("WorkspaceSize")
        if not _nested(volumes.get("input", {}), "configMap", "config_map"):
            mismatches.append("InputConfigMap")
        for volume in volumes.values():
            if _value(volume, "hostPath", "host_path") is not None:
                mismatches.append("HostPath")
            if _value(volume, "projected") is not None:
                mismatches.append("ProjectedToken")
        if _value(runner, "envFrom", "env_from"):
            mismatches.append("EnvironmentFrom")
        allowed_environment = {
            "TESTOPS_EXECUTOR_MODE",
            "TESTOPS_EXECUTOR_VERSION",
            "TESTOPS_RUNTIME_IMAGE_ID",
            "TESTOPS_NETWORK_POLICY",
            "TESTOPS_MEMORY_LIMIT_BYTES",
            "TESTOPS_CPU_LIMIT_MILLIS",
            "TESTOPS_EPHEMERAL_STORAGE_LIMIT_BYTES",
            "TESTOPS_ORCHESTRATOR_NAMESPACE",
            "TESTOPS_SERVICE_ACCOUNT_NAME",
            "TESTOPS_SERVICE_ACCOUNT_TOKEN_AUTOMOUNTED",
        }
        for item in _sequence(runner, "env"):
            name = str(_value(_as_mapping(item), "name"))
            if name not in allowed_environment and not name.startswith("TESTOPS_SECRET_"):
                mismatches.append("Environment")

    policy_spec = _nested(policy_document, "spec")
    if "Egress" not in _sequence(policy_spec, "policyTypes", "policy_types"):
        mismatches.append("NetworkPolicyType")
    egress = _sequence(policy_spec, "egress")
    if settings.runner_kubernetes_network_policy == "DENY_ALL" and egress:
        mismatches.append("DenyAllEgress")
    if settings.runner_kubernetes_network_policy == "ALLOWLIST" and not egress:
        mismatches.append("AllowlistEgress")
    policy_metadata = _nested(policy_document, "metadata")
    policy_labels = _nested(policy_metadata, "labels")
    selector = _nested(
        _nested(policy_spec, "podSelector", "pod_selector"), "matchLabels", "match_labels"
    )
    if selector.get("io.testops.run-id") != policy_labels.get("io.testops.run-id"):
        mismatches.append("NetworkPolicySelector")
    actual_cidrs: set[str] = set()
    for rule in egress:
        for peer in _sequence(_as_mapping(rule), "to"):
            ip_block = _nested(_as_mapping(peer), "ipBlock", "ip_block")
            if ip_block.get("cidr"):
                actual_cidrs.add(str(ip_block["cidr"]))
    if settings.runner_kubernetes_network_policy == "ALLOWLIST" and actual_cidrs != set(
        settings.runner_kubernetes_allow_cidrs
    ):
        mismatches.append("AllowlistCIDRs")

    statuses = _sequence(actual_status, "containerStatuses", "container_statuses")
    status = _as_mapping(statuses[0]) if len(statuses) == 1 else {}
    actual_digest = _image_digest(_value(status, "imageID", "image_id"))
    expected_digest = _image_digest(expected_image)
    if actual_digest is None or actual_digest != expected_digest:
        mismatches.append("RuntimeImageID")
    if mismatches:
        raise IsolatedExecutionError(
            "Kubernetes runtime does not match declared isolation: "
            + ", ".join(sorted(set(mismatches)))
        )
    return actual_digest


def _is_not_found(exc: Exception) -> bool:
    return getattr(exc, "status", None) == 404 or getattr(exc, "status_code", None) == 404


def _managed_for_run(resource: Any, run_id: object) -> bool:
    metadata = _nested(_as_mapping(resource), "metadata")
    labels = _nested(metadata, "labels")
    return labels.get("io.testops.managed") == "true" and labels.get("io.testops.run-id") == str(
        run_id
    )


def _reclaim_resource(
    read: Any,
    delete: Any,
    *,
    name: str,
    namespace: str,
    run_id: object,
    propagation_policy: str | None = None,
) -> None:
    try:
        resource = read(name=name, namespace=namespace)
    except Exception as exc:
        if _is_not_found(exc):
            return
        raise
    if not _managed_for_run(resource, run_id):
        raise IsolatedExecutionError(f"Kubernetes resource name conflict: {name}")
    kwargs: dict[str, object] = {"name": name, "namespace": namespace}
    if propagation_policy:
        kwargs["propagation_policy"] = propagation_policy
    delete(**kwargs)
    for _ in range(100):
        try:
            read(name=name, namespace=namespace)
        except Exception as exc:
            if _is_not_found(exc):
                return
            raise
        time.sleep(0.1)
    raise IsolatedExecutionError(f"Kubernetes resource deletion timed out: {name}")


def _pod_items(response: Any) -> Sequence[Any]:
    if isinstance(response, Mapping):
        items = response.get("items", ())
    else:
        items = getattr(response, "items", ())
    return items if isinstance(items, Sequence) else tuple(items or ())


def _pod_name_and_phase(pod: Any) -> tuple[str, str]:
    document = _as_mapping(pod)
    metadata = _nested(document, "metadata")
    status = _nested(document, "status")
    return str(metadata.get("name", "")), str(status.get("phase", ""))


def _pod_exec(
    bundle: KubernetesClientBundle, pod_name: str, namespace: str, command: list[str]
) -> str:
    try:
        output = bundle.stream(
            bundle.core.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            command=command,
            stderr=False,
            stdin=False,
            stdout=True,
            tty=False,
        )
    except Exception as exc:
        raise IsolatedExecutionError("Kubernetes Pod exec failed closed") from exc
    return output.decode("utf-8") if isinstance(output, bytes) else str(output)


def _forward_kubernetes_events(
    bundle: KubernetesClientBundle,
    pod_name: str,
    namespace: str,
    job: RunSnapshot,
    control_plane: ExecutionControlPlane,
    offset: int,
) -> int:
    path = f"{KUBERNETES_WORKSPACE_ROOT}/{job.run_id}/events.jsonl"
    size_text = _pod_exec(
        bundle,
        pod_name,
        namespace,
        [
            "/bin/sh",
            "-c",
            f"if [ -f {shlex.quote(path)} ]; then wc -c < {shlex.quote(path)}; else printf 0; fi",
        ],
    ).strip()
    try:
        size = int(size_text)
    except ValueError as exc:
        raise IsolatedExecutionError("Kubernetes event size is invalid") from exc
    if size > MAX_EVENT_ARCHIVE_BYTES:
        raise IsolatedExecutionError("Kubernetes event stream exceeded its size limit")
    if size <= offset:
        return offset
    payload = _pod_exec(bundle, pod_name, namespace, ["cat", path]).encode("utf-8")
    if len(payload) > MAX_EVENT_ARCHIVE_BYTES:
        raise IsolatedExecutionError("Kubernetes event stream exceeded its size limit")
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


def _export_run_archive(
    bundle: KubernetesClientBundle,
    pod_name: str,
    namespace: str,
    job: RunSnapshot,
) -> bytes:
    archive_path = f"{KUBERNETES_WORKSPACE_ROOT}/.testops-export-{job.run_id}.tar"
    create_command = (
        f"tar -C {shlex.quote(KUBERNETES_WORKSPACE_ROOT)} -cf {shlex.quote(archive_path)} "
        f"{shlex.quote(str(job.run_id))} && wc -c < {shlex.quote(archive_path)}"
    )
    size_text = _pod_exec(bundle, pod_name, namespace, ["/bin/sh", "-c", create_command]).strip()
    try:
        size = int(size_text)
    except ValueError as exc:
        raise IsolatedExecutionError("Kubernetes Run archive size is invalid") from exc
    if size <= 0 or size > MAX_RUN_ARCHIVE_BYTES:
        raise IsolatedExecutionError("Kubernetes Run archive exceeded its size limit")
    encoded = _pod_exec(bundle, pod_name, namespace, ["base64", archive_path])
    try:
        archive = base64.b64decode(encoded, validate=False)
    except (ValueError, binascii.Error) as exc:
        raise IsolatedExecutionError("Kubernetes Run archive is invalid") from exc
    if len(archive) != size:
        raise IsolatedExecutionError("Kubernetes Run archive is truncated")
    return archive


class KubernetesRunExecutor:
    def __init__(
        self,
        settings: KubernetesIsolationSettings,
        control_plane: ExecutionControlPlane,
        *,
        environ: Mapping[str, str] | None = None,
        clients: KubernetesClientBundle | None = None,
    ):
        self._settings = settings
        self._control_plane = control_plane
        self._environ = os.environ if environ is None else environ
        self._clients = clients

    def execute(self, job: RunSnapshot) -> RunResult:
        phase = "client"
        clients = self._clients or _kubernetes_clients(self._settings)
        namespace = self._settings.runner_kubernetes_namespace
        names = _resource_names(job)
        expected_image = _runtime_image(job)
        resources = kubernetes_resources(job, self._settings, environ=self._environ)
        created: list[str] = []
        isolation_verified = False
        expected_evidence: RunExecutionIsolationEvidence | None = None
        pod_name = ""
        try:
            phase = "stale-resource-recovery"
            _reclaim_resource(
                clients.batch.read_namespaced_job,
                clients.batch.delete_namespaced_job,
                name=names["job"],
                namespace=namespace,
                run_id=job.run_id,
                propagation_policy="Foreground",
            )
            _reclaim_resource(
                clients.networking.read_namespaced_network_policy,
                clients.networking.delete_namespaced_network_policy,
                name=names["network_policy"],
                namespace=namespace,
                run_id=job.run_id,
            )
            _reclaim_resource(
                clients.core.read_namespaced_secret,
                clients.core.delete_namespaced_secret,
                name=names["secret"],
                namespace=namespace,
                run_id=job.run_id,
            )
            _reclaim_resource(
                clients.core.read_namespaced_config_map,
                clients.core.delete_namespaced_config_map,
                name=names["input"],
                namespace=namespace,
                run_id=job.run_id,
            )
            phase = "input-create"
            clients.core.create_namespaced_config_map(
                namespace=namespace, body=resources["config_map"]
            )
            created.append("config_map")
            if resources["secret"] is not None:
                clients.core.create_namespaced_secret(namespace=namespace, body=resources["secret"])
                created.append("secret")
            phase = "network-policy-create"
            clients.networking.create_namespaced_network_policy(
                namespace=namespace, body=resources["network_policy"]
            )
            created.append("network_policy")
            phase = "job-create"
            clients.batch.create_namespaced_job(namespace=namespace, body=resources["job"])
            created.append("job")
            deadline = time.monotonic() + self._settings.runner_executor_timeout_seconds
            event_offset = 0
            while True:
                phase = "job-poll"
                pods = _pod_items(
                    clients.core.list_namespaced_pod(
                        namespace=namespace,
                        label_selector=f"io.testops.run-id={job.run_id}",
                    )
                )
                if len(pods) > 1:
                    raise IsolatedExecutionError("Kubernetes Job created multiple Run Pods")
                if pods:
                    pod = pods[0]
                    pod_name, pod_phase = _pod_name_and_phase(pod)
                    if pod_phase in {"Failed", "Succeeded"} and not isolation_verified:
                        raise IsolatedExecutionError(
                            f"Kubernetes Run Pod terminated before verification ({pod_phase})"
                        )
                    if pod_phase == "Running":
                        if not isolation_verified:
                            phase = "isolation-verification"
                            current_job = clients.batch.read_namespaced_job(
                                name=names["job"], namespace=namespace
                            )
                            current_policy = clients.networking.read_namespaced_network_policy(
                                name=names["network_policy"], namespace=namespace
                            )
                            runtime_image_id = verify_kubernetes_isolation(
                                current_job,
                                pod,
                                current_policy,
                                self._settings,
                                expected_image,
                            )
                            expected_evidence = kubernetes_isolation_evidence(
                                self._settings, runtime_image_id
                            )
                            isolation_verified = True
                        event_offset = _forward_kubernetes_events(
                            clients,
                            pod_name,
                            namespace,
                            job,
                            self._control_plane,
                            event_offset,
                        )
                        marker = _pod_exec(
                            clients,
                            pod_name,
                            namespace,
                            [
                                "/bin/sh",
                                "-c",
                                FINISHED_COMMAND,
                            ],
                        ).strip()
                        if marker != "pending":
                            if marker != "0":
                                raise IsolatedExecutionError(
                                    f"Kubernetes executor exited without a result (code {marker})"
                                )
                            phase = "result-export"
                            archive = _export_run_archive(clients, pod_name, namespace, job)
                            extract_run_archive(
                                [archive],
                                workspace_root=self._settings.workspace_root,
                                run_id=job.run_id,
                            )
                            result = _read_result(self._settings.workspace_root, job)
                            if result.execution_isolation != expected_evidence:
                                raise IsolatedExecutionError(
                                    "Kubernetes executor isolation evidence is inconsistent"
                                )
                            return result
                if self._control_plane.cancel_requested(job.run_id):
                    raise IsolatedExecutionCanceled("isolated Kubernetes Run was canceled")
                if time.monotonic() >= deadline:
                    raise IsolatedExecutionTimedOut(
                        "isolated Kubernetes Run exceeded executor timeout"
                    )
                time.sleep(self._settings.runner_executor_poll_seconds)
        except (
            IsolatedExecutionCanceled,
            IsolatedExecutionTimedOut,
            IsolatedExecutionError,
        ) as exc:
            if isolation_verified and expected_evidence is not None:
                exc.execution_isolation = expected_evidence
            raise
        except Exception as exc:
            raise IsolatedExecutionError(
                f"Kubernetes executor failed closed during {phase}"
            ) from exc
        finally:
            if "job" in created:
                try:
                    clients.batch.delete_namespaced_job(
                        name=names["job"],
                        namespace=namespace,
                        propagation_policy="Background",
                    )
                except Exception:
                    pass
            if "network_policy" in created:
                try:
                    clients.networking.delete_namespaced_network_policy(
                        name=names["network_policy"], namespace=namespace
                    )
                except Exception:
                    pass
            if "secret" in created:
                try:
                    clients.core.delete_namespaced_secret(name=names["secret"], namespace=namespace)
                except Exception:
                    pass
            if "config_map" in created:
                try:
                    clients.core.delete_namespaced_config_map(
                        name=names["input"], namespace=namespace
                    )
                except Exception:
                    pass
