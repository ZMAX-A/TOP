"""Worker settings sourced from the process environment."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from ipaddress import ip_network

from .package_runtime import PackageRuntimeCatalog


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    database_url: str
    broker_url: str
    control_plane_url: str
    runner_callback_token: str = field(repr=False)
    workspace_root: str
    outbox_batch_size: int
    minio_endpoint: str | None
    minio_access_key: str | None = field(repr=False)
    minio_secret_key: str | None = field(repr=False)
    minio_bucket: str
    minio_region: str
    runner_worker_key: str | None
    runner_pool_key: str | None
    runner_display_name: str
    runner_version: str
    runner_max_slots: int
    runner_capabilities: dict[str, object]
    runner_package_catalog: PackageRuntimeCatalog
    runner_execution_mode: str
    runner_executor_poll_seconds: float
    runner_executor_timeout_seconds: int
    runner_container_image: str | None
    runner_container_network_policy: str
    runner_container_network: str | None
    runner_container_memory_bytes: int
    runner_container_cpu_millis: int
    runner_container_pids_limit: int
    runner_container_shm_bytes: int
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
    runner_heartbeat_interval_seconds: float
    runner_heartbeat_ttl_seconds: int
    runner_capacity_poll_seconds: float

    @classmethod
    def from_environment(cls) -> WorkerSettings:
        token = os.getenv("RUNNER_CALLBACK_TOKEN", "")
        if not token:
            raise RuntimeError("RUNNER_CALLBACK_TOKEN is required for the Worker")
        batch_size = int(os.getenv("OUTBOX_BATCH_SIZE", "20"))
        if not 1 <= batch_size <= 500:
            raise RuntimeError("OUTBOX_BATCH_SIZE must be between 1 and 500")
        worker_key = os.getenv("RUNNER_WORKER_KEY") or None
        pool_key = os.getenv("RUNNER_POOL_KEY") or None
        if (worker_key is None) != (pool_key is None):
            raise RuntimeError("RUNNER_WORKER_KEY and RUNNER_POOL_KEY must be configured together")
        max_slots = int(os.getenv("RUNNER_MAX_SLOTS", "1"))
        if not 1 <= max_slots <= 100:
            raise RuntimeError("RUNNER_MAX_SLOTS must be between 1 and 100")
        heartbeat_interval = float(os.getenv("RUNNER_HEARTBEAT_INTERVAL_SECONDS", "15"))
        if not 5 <= heartbeat_interval <= 300:
            raise RuntimeError("RUNNER_HEARTBEAT_INTERVAL_SECONDS must be between 5 and 300")
        heartbeat_ttl = int(os.getenv("RUNNER_HEARTBEAT_TTL_SECONDS", "45"))
        if not 15 <= heartbeat_ttl <= 600:
            raise RuntimeError("RUNNER_HEARTBEAT_TTL_SECONDS must be between 15 and 600")
        capacity_poll = float(os.getenv("RUNNER_CAPACITY_POLL_SECONDS", "2"))
        if not 0.1 <= capacity_poll <= 60:
            raise RuntimeError("RUNNER_CAPACITY_POLL_SECONDS must be between 0.1 and 60")
        try:
            capabilities = json.loads(
                os.getenv(
                    "RUNNER_CAPABILITIES",
                    '{"target_types":["WEB"],"browsers":["chromium"],"labels":{}}',
                )
            )
        except json.JSONDecodeError as exc:
            raise RuntimeError("RUNNER_CAPABILITIES must be valid JSON") from exc
        if not isinstance(capabilities, dict):
            raise RuntimeError("RUNNER_CAPABILITIES must be a JSON object")
        if "automation_packages" in capabilities:
            raise RuntimeError(
                "automation_packages must be configured through RUNNER_PACKAGE_CATALOG"
            )
        if "execution_isolation" in capabilities:
            raise RuntimeError(
                "execution_isolation is derived from RUNNER_EXECUTION_MODE and cannot be overridden"
            )
        package_catalog = PackageRuntimeCatalog.from_json(os.getenv("RUNNER_PACKAGE_CATALOG", "[]"))
        if worker_key is not None and not package_catalog.packages:
            raise RuntimeError("RUNNER_PACKAGE_CATALOG is required for a registered Runner Worker")
        execution_mode = os.getenv("RUNNER_EXECUTION_MODE", "SUBPROCESS").strip().upper()
        if execution_mode not in {"IN_PROCESS", "SUBPROCESS", "CONTAINER", "KUBERNETES"}:
            raise RuntimeError(
                "RUNNER_EXECUTION_MODE must be IN_PROCESS, SUBPROCESS, CONTAINER or KUBERNETES"
            )
        executor_poll_seconds = float(os.getenv("RUNNER_EXECUTOR_POLL_SECONDS", "2"))
        if not 0.1 <= executor_poll_seconds <= 10:
            raise RuntimeError("RUNNER_EXECUTOR_POLL_SECONDS must be between 0.1 and 10")
        executor_timeout_seconds = int(os.getenv("RUNNER_EXECUTOR_TIMEOUT_SECONDS", "3600"))
        if not 60 <= executor_timeout_seconds <= 86400:
            raise RuntimeError("RUNNER_EXECUTOR_TIMEOUT_SECONDS must be between 60 and 86400")
        container_image = os.getenv("RUNNER_CONTAINER_IMAGE", "").strip() or None
        if execution_mode == "CONTAINER" and container_image is None:
            raise RuntimeError("RUNNER_CONTAINER_IMAGE is required for CONTAINER execution")
        container_network_policy = (
            os.getenv("RUNNER_CONTAINER_NETWORK_POLICY", "DENY_ALL").strip().upper()
        )
        if container_network_policy not in {"DENY_ALL", "ALLOWLIST"}:
            raise RuntimeError("RUNNER_CONTAINER_NETWORK_POLICY must be DENY_ALL or ALLOWLIST")
        container_network = os.getenv("RUNNER_CONTAINER_NETWORK", "").strip() or None
        if container_network is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container_network
        ):
            raise RuntimeError("RUNNER_CONTAINER_NETWORK has an invalid Docker network name")
        if container_network_policy == "ALLOWLIST" and container_network is None:
            raise RuntimeError("RUNNER_CONTAINER_NETWORK is required for ALLOWLIST")
        container_memory_mib = int(os.getenv("RUNNER_CONTAINER_MEMORY_MIB", "1024"))
        if not 256 <= container_memory_mib <= 32768:
            raise RuntimeError("RUNNER_CONTAINER_MEMORY_MIB must be between 256 and 32768")
        container_cpu_millis = int(os.getenv("RUNNER_CONTAINER_CPU_MILLIS", "1000"))
        if not 100 <= container_cpu_millis <= 32000:
            raise RuntimeError("RUNNER_CONTAINER_CPU_MILLIS must be between 100 and 32000")
        container_pids_limit = int(os.getenv("RUNNER_CONTAINER_PIDS_LIMIT", "256"))
        if not 16 <= container_pids_limit <= 4096:
            raise RuntimeError("RUNNER_CONTAINER_PIDS_LIMIT must be between 16 and 4096")
        container_shm_mib = int(os.getenv("RUNNER_CONTAINER_SHM_MIB", "512"))
        if not 64 <= container_shm_mib <= 4096:
            raise RuntimeError("RUNNER_CONTAINER_SHM_MIB must be between 64 and 4096")
        kubernetes_namespace = os.getenv("RUNNER_KUBERNETES_NAMESPACE", "testops-runs").strip()
        kubernetes_service_account = os.getenv(
            "RUNNER_KUBERNETES_SERVICE_ACCOUNT", "testops-runner"
        ).strip()
        dns_label = r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?"
        if not re.fullmatch(dns_label, kubernetes_namespace):
            raise RuntimeError("RUNNER_KUBERNETES_NAMESPACE must be a Kubernetes DNS label")
        if not re.fullmatch(dns_label, kubernetes_service_account):
            raise RuntimeError("RUNNER_KUBERNETES_SERVICE_ACCOUNT must be a Kubernetes DNS label")
        if execution_mode == "KUBERNETES" and kubernetes_service_account == "default":
            raise RuntimeError("Kubernetes execution cannot use the default ServiceAccount")
        kubernetes_network_policy = (
            os.getenv("RUNNER_KUBERNETES_NETWORK_POLICY", "DENY_ALL").strip().upper()
        )
        if kubernetes_network_policy not in {"DENY_ALL", "ALLOWLIST"}:
            raise RuntimeError("RUNNER_KUBERNETES_NETWORK_POLICY must be DENY_ALL or ALLOWLIST")
        kubernetes_network_policy_enforced = _environment_boolean(
            "RUNNER_KUBERNETES_NETWORK_POLICY_ENFORCED", default=False
        )
        if execution_mode == "KUBERNETES" and not kubernetes_network_policy_enforced:
            raise RuntimeError(
                "RUNNER_KUBERNETES_NETWORK_POLICY_ENFORCED=true is required for "
                "Kubernetes execution"
            )
        raw_allow_cidrs = os.getenv("RUNNER_KUBERNETES_ALLOW_CIDRS", "")
        try:
            kubernetes_allow_cidrs = tuple(
                str(ip_network(value.strip(), strict=False))
                for value in raw_allow_cidrs.split(",")
                if value.strip()
            )
        except ValueError as exc:
            raise RuntimeError("RUNNER_KUBERNETES_ALLOW_CIDRS contains an invalid CIDR") from exc
        if len(kubernetes_allow_cidrs) != len(set(kubernetes_allow_cidrs)):
            raise RuntimeError("RUNNER_KUBERNETES_ALLOW_CIDRS contains duplicates")
        if (
            execution_mode == "KUBERNETES"
            and kubernetes_network_policy == "ALLOWLIST"
            and not kubernetes_allow_cidrs
        ):
            raise RuntimeError("RUNNER_KUBERNETES_ALLOW_CIDRS is required for ALLOWLIST")
        kubernetes_memory_mib = int(os.getenv("RUNNER_KUBERNETES_MEMORY_MIB", "1024"))
        if not 256 <= kubernetes_memory_mib <= 32768:
            raise RuntimeError("RUNNER_KUBERNETES_MEMORY_MIB must be between 256 and 32768")
        kubernetes_cpu_millis = int(os.getenv("RUNNER_KUBERNETES_CPU_MILLIS", "1000"))
        if not 100 <= kubernetes_cpu_millis <= 32000:
            raise RuntimeError("RUNNER_KUBERNETES_CPU_MILLIS must be between 100 and 32000")
        kubernetes_ephemeral_storage_mib = int(
            os.getenv("RUNNER_KUBERNETES_EPHEMERAL_STORAGE_MIB", "2048")
        )
        if not 256 <= kubernetes_ephemeral_storage_mib <= 65536:
            raise RuntimeError(
                "RUNNER_KUBERNETES_EPHEMERAL_STORAGE_MIB must be between 256 and 65536"
            )
        kubernetes_cleanup_ttl_seconds = int(
            os.getenv("RUNNER_KUBERNETES_CLEANUP_TTL_SECONDS", "300")
        )
        if not 60 <= kubernetes_cleanup_ttl_seconds <= 86400:
            raise RuntimeError("RUNNER_KUBERNETES_CLEANUP_TTL_SECONDS must be between 60 and 86400")
        kubernetes_in_cluster = _environment_boolean("RUNNER_KUBERNETES_IN_CLUSTER", default=True)
        kubernetes_context = os.getenv("RUNNER_KUBERNETES_CONTEXT", "").strip() or None
        if kubernetes_in_cluster and kubernetes_context is not None:
            raise RuntimeError(
                "RUNNER_KUBERNETES_CONTEXT is only valid when RUNNER_KUBERNETES_IN_CLUSTER=false"
            )
        capabilities["automation_packages"] = package_catalog.capability_payload()
        if execution_mode == "CONTAINER":
            capabilities["execution_isolation"] = {
                "mode": execution_mode,
                "dedicated_process": True,
                "credential_scope": "RUN_SECRETS_ONLY",
                "read_only_root_filesystem": True,
                "network_policy": container_network_policy,
                "resource_limits_enforced": True,
                "memory_limit_bytes": container_memory_mib * 1024 * 1024,
                "cpu_limit_millis": container_cpu_millis,
                "pids_limit": container_pids_limit,
            }
        elif execution_mode == "KUBERNETES":
            capabilities["execution_isolation"] = {
                "mode": execution_mode,
                "dedicated_process": True,
                "credential_scope": "RUN_SECRETS_ONLY",
                "read_only_root_filesystem": True,
                "network_policy": kubernetes_network_policy,
                "resource_limits_enforced": True,
                "memory_limit_bytes": kubernetes_memory_mib * 1024 * 1024,
                "cpu_limit_millis": kubernetes_cpu_millis,
                "ephemeral_storage_limit_bytes": kubernetes_ephemeral_storage_mib * 1024 * 1024,
                "orchestrator_namespace": kubernetes_namespace,
                "service_account_name": kubernetes_service_account,
                "service_account_token_automounted": False,
            }
        else:
            capabilities["execution_isolation"] = {
                "mode": execution_mode,
                "dedicated_process": execution_mode == "SUBPROCESS",
                "credential_scope": (
                    "RUN_SECRETS_ONLY" if execution_mode == "SUBPROCESS" else "WORKER"
                ),
                "read_only_root_filesystem": False,
                "network_policy": "WORKER_DEFAULT",
                "resource_limits_enforced": False,
            }
        return cls(
            database_url=os.getenv(
                "DATABASE_URL",
                "postgresql+asyncpg://testops:change-me-local-only@localhost:5432/testops",
            ),
            broker_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            control_plane_url=os.getenv("CONTROL_PLANE_URL", "http://127.0.0.1:8000"),
            runner_callback_token=token,
            workspace_root=os.getenv("RUNNER_WORKSPACE_ROOT", "artifacts/runs"),
            outbox_batch_size=batch_size,
            minio_endpoint=os.getenv("MINIO_ENDPOINT") or None,
            minio_access_key=os.getenv("MINIO_ACCESS_KEY") or os.getenv("MINIO_ROOT_USER") or None,
            minio_secret_key=os.getenv("MINIO_SECRET_KEY")
            or os.getenv("MINIO_ROOT_PASSWORD")
            or None,
            minio_bucket=os.getenv("MINIO_BUCKET", "testops-artifacts"),
            minio_region=os.getenv("MINIO_REGION", "us-east-1"),
            runner_worker_key=worker_key,
            runner_pool_key=pool_key,
            runner_display_name=os.getenv("RUNNER_DISPLAY_NAME", worker_key or "Legacy Worker"),
            runner_version=os.getenv("RUNNER_VERSION", "0.30.0"),
            runner_max_slots=max_slots,
            runner_capabilities=capabilities,
            runner_package_catalog=package_catalog,
            runner_execution_mode=execution_mode,
            runner_executor_poll_seconds=executor_poll_seconds,
            runner_executor_timeout_seconds=executor_timeout_seconds,
            runner_container_image=container_image,
            runner_container_network_policy=container_network_policy,
            runner_container_network=container_network,
            runner_container_memory_bytes=container_memory_mib * 1024 * 1024,
            runner_container_cpu_millis=container_cpu_millis,
            runner_container_pids_limit=container_pids_limit,
            runner_container_shm_bytes=container_shm_mib * 1024 * 1024,
            runner_kubernetes_namespace=kubernetes_namespace,
            runner_kubernetes_service_account=kubernetes_service_account,
            runner_kubernetes_network_policy=kubernetes_network_policy,
            runner_kubernetes_network_policy_enforced=kubernetes_network_policy_enforced,
            runner_kubernetes_allow_cidrs=kubernetes_allow_cidrs,
            runner_kubernetes_memory_bytes=kubernetes_memory_mib * 1024 * 1024,
            runner_kubernetes_cpu_millis=kubernetes_cpu_millis,
            runner_kubernetes_ephemeral_storage_bytes=kubernetes_ephemeral_storage_mib
            * 1024
            * 1024,
            runner_kubernetes_cleanup_ttl_seconds=kubernetes_cleanup_ttl_seconds,
            runner_kubernetes_in_cluster=kubernetes_in_cluster,
            runner_kubernetes_context=kubernetes_context,
            runner_heartbeat_interval_seconds=heartbeat_interval,
            runner_heartbeat_ttl_seconds=heartbeat_ttl,
            runner_capacity_poll_seconds=capacity_poll,
        )


def _environment_boolean(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")
