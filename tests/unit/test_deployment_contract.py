from pathlib import Path

import pytest
from scripts.smoke_compose import (
    FAIL_CASE_CODE,
    PASS_CASE_CODE,
    ApiClient,
    SmokeError,
    build_smoke_baseline,
    runner_source_digest,
    validate_run,
)

from testops.contracts import CaseBaseline, canonical_sha256
from testops.runners.web.engine import WebCaseEngine

ROOT = Path(__file__).resolve().parents[2]


def test_compose_declares_the_complete_runtime_topology() -> None:
    compose = (ROOT / "compose.yaml").read_text("utf-8")
    for service in (
        "postgres",
        "redis",
        "minio",
        "minio-init",
        "migrate",
        "api",
        "outbox",
        "scheduler",
        "reaper",
        "worker",
        "frontend",
        "smoke-target",
    ):
        assert f"  {service}:" in compose
    assert "condition: service_completed_successfully" in compose
    assert 'profiles: ["smoke"]' in compose
    assert "runner-workspaces:/var/lib/testops/runs" in compose
    assert "RUNNER_WORKER_KEY:" in compose
    assert "RUNNER_POOL_KEY:" in compose
    assert "RUNNER_CAPABILITIES:" in compose
    assert "RUNNER_PACKAGE_CATALOG:" in compose
    assert "RUNNER_EXECUTION_MODE: ${RUNNER_EXECUTION_MODE:-CONTAINER}" in compose
    assert "RUNNER_EXECUTOR_TIMEOUT_SECONDS:" in compose
    assert (
        "RUNNER_CONTAINER_IMAGE: "
        "${RUNNER_CONTAINER_IMAGE:-testops-worker:${TESTOPS_IMAGE_TAG:-0.30.0}}" in compose
    )
    assert (
        "RUNNER_CONTAINER_NETWORK_POLICY: ${RUNNER_CONTAINER_NETWORK_POLICY:-ALLOWLIST}" in compose
    )
    assert "RUNNER_CONTAINER_MEMORY_MIB:" in compose
    assert "RUNNER_CONTAINER_CPU_MILLIS:" in compose
    assert "RUNNER_CONTAINER_PIDS_LIMIT:" in compose
    for setting in (
        "SUPPLY_CHAIN_POLICY_VERSION",
        "SUPPLY_CHAIN_ALLOWED_VERIFIERS",
        "SUPPLY_CHAIN_ALLOWED_CERTIFICATE_ISSUERS",
        "SUPPLY_CHAIN_ALLOWED_CERTIFICATE_IDENTITIES",
        "SUPPLY_CHAIN_ALLOWED_BUILDER_IDS",
        "SUPPLY_CHAIN_ALLOWED_SOURCE_REPOSITORIES",
    ):
        assert f"{setting}:" in compose
    assert "x-api-environment: &api-environment" in compose
    assert "SUPPLY_CHAIN_VERIFIER_ED25519_KEYS:" in compose
    assert "SUPPLY_CHAIN_ALLOW_LEGACY_HMAC:" in compose
    assert "SUPPLY_CHAIN_VERIFIER_HMAC_KEYS:" in compose
    assert "SUPPLY_CHAIN_ENVELOPE_TTL_SECONDS:" in compose
    assert "SUPPLY_CHAIN_ENVELOPE_FUTURE_SKEW_SECONDS:" in compose
    assert "environment: *api-environment" in compose
    common_environment = compose.split("x-control-plane-environment:", 1)[1].split(
        "x-worker-environment:",
        1,
    )[0]
    api_environment = compose.split("x-api-environment:", 1)[1].split("services:", 1)[0]
    for verifier_setting in (
        "SUPPLY_CHAIN_VERIFIER_ED25519_KEYS",
        "SUPPLY_CHAIN_ALLOW_LEGACY_HMAC",
        "SUPPLY_CHAIN_VERIFIER_HMAC_KEYS",
    ):
        assert verifier_setting not in common_environment
        assert verifier_setting in api_environment
    assert "/var/run/docker.sock:/var/run/docker.sock" in compose
    assert "internal: true" in compose
    assert '"image_repository":"testops-worker"' in compose
    assert "--queues=celery,testops.pool.${RUNNER_POOL_KEY:-default-web}" in compose
    assert "scripts/schedule_regressions.py" in compose
    assert "scripts/reap_run_reliability.py" in compose

    workflow = (ROOT / ".github/workflows/ci.yml").read_text("utf-8")
    assert "stat -c '%g' /var/run/docker.sock" in workflow
    assert 'echo "DOCKER_SOCKET_GID=${docker_socket_gid}" >> "${GITHUB_ENV}"' in workflow

    for path in (
        "infra/docker/api.Dockerfile",
        "infra/docker/worker.Dockerfile",
        "infra/docker/frontend.Dockerfile",
        "infra/docker/nginx.conf",
        "infra/smoke-target/Dockerfile",
        "infra/smoke-target/html/login.html",
    ):
        assert (ROOT / path).is_file()


def test_frontend_proxy_preserves_authenticated_sse_streaming() -> None:
    nginx = (ROOT / "infra/docker/nginx.conf").read_text("utf-8")
    assert "proxy_pass http://api:8000;" in nginx
    assert "proxy_buffering off;" in nginx
    assert "proxy_read_timeout 3600s;" in nginx
    assert "client_max_body_size 1m;" in nginx
    assert "server_tokens off;" in nginx


def test_container_base_images_are_pinned_and_bootstrap_can_be_disabled() -> None:
    compose = (ROOT / "compose.yaml").read_text("utf-8")
    dockerfiles = "\n".join(
        (ROOT / path).read_text("utf-8")
        for path in (
            "infra/docker/api.Dockerfile",
            "infra/docker/worker.Dockerfile",
            "infra/docker/frontend.Dockerfile",
            "infra/smoke-target/Dockerfile",
        )
    )
    assert "BOOTSTRAP_ADMIN_TOKEN: ${BOOTSTRAP_ADMIN_TOKEN-}" in compose
    assert "change-me-one-time-bootstrap-token" not in compose
    assert compose.count("@sha256:") >= 5
    assert dockerfiles.count("@sha256:") == 5


def test_compose_smoke_baseline_is_stable_and_contract_valid() -> None:
    baseline = build_smoke_baseline()
    validated = CaseBaseline.model_validate(baseline.model_dump(mode="json"))
    assert [case.case_code for case in validated.cases] == [
        PASS_CASE_CODE,
        FAIL_CASE_CODE,
    ]
    assert canonical_sha256(validated) == canonical_sha256(build_smoke_baseline())
    engine = WebCaseEngine()
    for case in validated.cases:
        engine.validate_case(case)
    serialized = validated.model_dump_json()
    assert "smoke-password" not in serialized
    assert "$" + "{SMOKE_PASSWORD}" in serialized
    compose = (ROOT / "compose.yaml").read_text("utf-8")
    assert runner_source_digest() in compose


def test_compose_smoke_requires_container_isolation_evidence() -> None:
    smoke = (ROOT / "scripts/smoke_compose.py").read_text("utf-8")
    assert '"mode": "CONTAINER"' in smoke
    assert '"executor_version": PACKAGE_VERSION' in smoke
    assert '"credential_scope": "RUN_SECRETS_ONLY"' in smoke
    assert '"read_only_root_filesystem": True' in smoke
    assert '"resource_limits_enforced": True' in smoke
    assert 'runtime_image_id = isolation.get("runtime_image_id")' in smoke
    assert 'os.getenv("TESTOPS_DOCKER_EXECUTABLE"' in smoke


def test_compose_smoke_reports_bounded_terminal_diagnostics_without_leaking_secrets() -> None:
    detail = {
        "id": "00000000-0000-0000-0000-000000000001",
        "status": "INFRA_ERROR",
        "result": {
            "case_results": [
                {
                    "case_code": "TC-SMOKE-PASS",
                    "failure_category": "EXECUTOR_ISOLATION",
                    "error_message": "Docker daemon is unavailable to the container executor",
                }
            ]
        },
    }
    with pytest.raises(SmokeError) as captured:
        validate_run(
            ApiClient("http://127.0.0.1:1"),
            {},
            detail,
            expected_status="PASSED",
            secret_value="smoke-password",
        )
    message = str(captured.value)
    assert "EXECUTOR_ISOLATION" in message
    assert "Docker daemon is unavailable" in message

    detail["result"]["case_results"][0]["error_message"] = "smoke-password"
    with pytest.raises(SmokeError, match="response leaked a bound secret value"):
        validate_run(
            ApiClient("http://127.0.0.1:1"),
            {},
            detail,
            expected_status="PASSED",
            secret_value="smoke-password",
        )


def test_kubernetes_runner_template_scopes_controller_and_run_identities() -> None:
    manifest = (ROOT / "infra/kubernetes/m9.5.3-runner.yaml").read_text("utf-8")
    executor = (ROOT / "services/worker/src/testops/worker/kubernetes_execution.py").read_text(
        "utf-8"
    )
    worker_dockerfile = (ROOT / "infra/docker/worker.Dockerfile").read_text("utf-8")
    api_dockerfile = (ROOT / "infra/docker/api.Dockerfile").read_text("utf-8")

    assert "pod-security.kubernetes.io/enforce: restricted" in manifest
    assert "kind: ClusterRole" not in manifest
    assert "kind: Role" in manifest
    assert 'resources: ["pods/exec"]' in manifest
    assert "name: testops-controller" in manifest
    assert "name: testops-runner" in manifest
    assert "automountServiceAccountToken: false" in manifest
    assert "expirationSeconds: 3600" in manifest
    assert "RUNNER_EXECUTION_MODE: KUBERNETES" in manifest
    assert 'RUNNER_KUBERNETES_NETWORK_POLICY_ENFORCED: "true"' in manifest
    assert "registry.example.invalid/testops/controller@sha256:" in manifest
    assert "docker.sock" not in manifest
    assert "hostPath" not in manifest
    assert "privileged: true" not in manifest
    assert "secretRef:" in manifest
    assert "RUNNER_CALLBACK_TOKEN:" not in manifest
    assert ".[kubernetes-executor,platform,runner]" in worker_dockerfile
    assert "kubernetes-executor" not in api_dockerfile

    for boundary in (
        '"automountServiceAccountToken": False',
        '"readOnlyRootFilesystem": True',
        '"allowPrivilegeEscalation": False',
        '"capabilities": {"drop": ["ALL"]}',
        '"hostNetwork": False',
        '"hostPID": False',
        '"hostIPC": False',
        '"ephemeral-storage"',
        '"NetworkPolicy"',
    ):
        assert boundary in executor
