from pathlib import Path

from scripts.smoke_compose import FAIL_CASE_CODE, PASS_CASE_CODE, build_smoke_baseline

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
    assert "--queues=celery,testops.pool.${RUNNER_POOL_KEY:-default-web}" in compose
    assert "scripts/schedule_regressions.py" in compose
    assert "scripts/reap_run_reliability.py" in compose

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
