"""Exercise the complete Compose control-plane, queue, Runner and artifact path."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid4, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/contracts/src"))

from testops.contracts import (  # noqa: E402
    AssertionDefinition,
    CaseBaseline,
    CaseBaselineSource,
    CaseDefinition,
    CasePriority,
    StepDefinition,
    canonical_sha256,
)

TERMINAL_STATUSES = {"PASSED", "FAILED", "CANCELED", "TIMED_OUT", "INFRA_ERROR"}
PROJECT_KEY = "platform-smoke"
TARGET_KEY = "web"
ENVIRONMENT_KEY = "compose"
PACKAGE_NAME = "compose-smoke-runner"
PACKAGE_VERSION = "0.30.0"
PASS_CASE_CODE = "TC-SMOKE-PASS"
FAIL_CASE_CODE = "TC-SMOKE-FAIL"


def runner_source_digest() -> str:
    source_root = ROOT / "runners/web_playwright/src"
    digest = hashlib.sha256()
    for path in sorted(source_root.rglob("*.py")):
        digest.update(path.relative_to(source_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


class SmokeError(RuntimeError):
    """The full-stack smoke contract was not satisfied."""


@dataclass(frozen=True, slots=True)
class SmokeResources:
    project_id: str
    target_id: str
    environment_id: str
    baseline_id: str
    package_id: str


class ApiClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        headers: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> tuple[int, Any]:
        body = None
        request_headers = {"Accept": "application/json", **(headers or {})}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                status = response.status
                raw = response.read()
                content_type = response.headers.get_content_type()
        except HTTPError as exc:
            status = exc.code
            raw = exc.read()
            content_type = exc.headers.get_content_type()
        except (OSError, URLError) as exc:
            raise SmokeError(f"{method} {path} could not connect: {exc}") from exc
        decoded = _decode_body(raw, content_type)
        if status not in expected:
            raise SmokeError(
                f"{method} {path} returned HTTP {status}, expected {expected}: {decoded}"
            )
        return status, decoded

    def download(self, url: str) -> bytes:
        request = Request(url, headers={"Accept": "application/octet-stream"}, method="GET")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                if response.status != 200:
                    raise SmokeError(f"artifact download returned HTTP {response.status}")
                return response.read()
        except (HTTPError, OSError, URLError) as exc:
            raise SmokeError(f"artifact download failed: {exc}") from exc


def _decode_body(raw: bytes, content_type: str) -> Any:
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    if content_type == "application/json":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SmokeError(f"{context} did not return a JSON object")
    return value


def _objects(value: Any, context: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise SmokeError(f"{context} did not return a JSON object list")
    return value


def _setting(name: str, default: str) -> str:
    environment_value = os.getenv(name)
    if environment_value is not None:
        return environment_value
    dotenv_path = ROOT / ".env"
    if not dotenv_path.is_file():
        return default
    for raw_line in dotenv_path.read_text("utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key.strip() == name:
            normalized = value.strip()
            if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in "\"'":
                normalized = normalized[1:-1]
            return normalized
    return default


def build_smoke_baseline() -> CaseBaseline:
    """Return a stable two-case baseline for success and artifact paths."""

    success = CaseDefinition(
        case_id=uuid5(NAMESPACE_URL, "testops-platform/compose-smoke/pass"),
        case_code=PASS_CASE_CODE,
        module_key="login",
        module_name="Compose 登录",
        title="Compose 登录成功",
        test_point="验证真实 Worker 可以完成浏览器登录",
        priority=CasePriority.P0,
        preconditions=("打开登录页面",),
        steps=(
            StepDefinition(
                operation="input",
                locator="#username",
                input="$" + "{SMOKE_USERNAME}",
            ),
            StepDefinition(
                operation="input",
                locator="#password",
                input="$" + "{SMOKE_PASSWORD}",
            ),
            StepDefinition(operation="click", locator="button[type='submit']"),
        ),
        assertion=AssertionDefinition(
            type="url_contains",
            expected="/dashboard",
        ),
        tags=("compose-smoke", "path:success"),
        timeout_seconds=15,
    )
    intentional_failure = CaseDefinition(
        case_id=uuid5(NAMESPACE_URL, "testops-platform/compose-smoke/fail"),
        case_code=FAIL_CASE_CODE,
        module_key="login",
        module_name="Compose 登录",
        title="Compose 制品采集",
        test_point="通过预期失败验证 Screenshot 和 Trace 上传",
        priority=CasePriority.P1,
        preconditions=("打开登录页面",),
        steps=(
            StepDefinition(
                operation="input",
                locator="#username",
                input="$" + "{SMOKE_USERNAME}",
            ),
            StepDefinition(
                operation="input",
                locator="#password",
                input="$" + "{SMOKE_PASSWORD}",
            ),
            StepDefinition(operation="click", locator="button[type='submit']"),
        ),
        assertion=AssertionDefinition(
            type="text_visible",
            expected="INTENTIONAL_SMOKE_ASSERTION_FAILURE",
            locator="body",
        ),
        tags=("compose-smoke", "path:artifact"),
        timeout_seconds=5,
    )
    source_digest = "sha256:" + hashlib.sha256(b"testops-compose-smoke-v1").hexdigest()
    return CaseBaseline(
        baseline_id=uuid5(NAMESPACE_URL, "testops-platform/compose-smoke/baseline-v0.30.0"),
        project_key=PROJECT_KEY,
        version="case-v0.30.0",
        source=CaseBaselineSource(
            file_name="compose-smoke.json",
            file_digest=source_digest,
            worksheet="compose-smoke",
        ),
        cases=(success, intentional_failure),
    )


def wait_until_ready(client: ApiClient, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = "not checked"
    while time.monotonic() < deadline:
        try:
            _, payload = client.request("GET", "/readyz")
            ready = _object(payload, "GET /readyz")
            if ready.get("status") == "ready" and ready.get("database") is True:
                client.request("GET", "/")
                return
            last_error = str(ready)
        except SmokeError as exc:
            last_error = str(exc)
        time.sleep(1)
    raise SmokeError(f"stack did not become ready within {timeout_seconds}s: {last_error}")


def wait_for_http_url(url: str, timeout_seconds: float, *, label: str) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = "not checked"
    while time.monotonic() < deadline:
        try:
            with urlopen(Request(url, method="GET"), timeout=5) as response:
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
        except (HTTPError, OSError, URLError) as exc:
            last_error = str(exc)
        time.sleep(1)
    raise SmokeError(f"{label} did not become ready within {timeout_seconds}s: {last_error}")


def authenticate(
    client: ApiClient,
    *,
    bootstrap_token: str,
    username: str,
    password: str,
) -> dict[str, str]:
    client.request(
        "POST",
        "/api/v1/auth/bootstrap",
        payload={
            "username": username,
            "display_name": "Compose Smoke Admin",
            "password": password,
        },
        headers={"X-Bootstrap-Token": bootstrap_token},
        expected=(201, 409),
    )
    _, payload = client.request(
        "POST",
        "/api/v1/auth/login",
        payload={"username": username, "password": password},
    )
    session = _object(payload, "POST /api/v1/auth/login")
    token = session.get("access_token")
    if not isinstance(token, str) or not token:
        raise SmokeError("login response did not contain an access token")
    return {"Authorization": f"Bearer {token}"}


def ensure_resources(
    client: ApiClient,
    headers: dict[str, str],
    *,
    target_url: str,
) -> SmokeResources:
    runner_pool_key = _setting("RUNNER_POOL_KEY", "default-web")
    _, payload = client.request("GET", "/api/v1/admin/runner-pools", headers=headers)
    runner_pools = _objects(payload, "GET /api/v1/admin/runner-pools")
    runner_pool = next(
        (item for item in runner_pools if item.get("key") == runner_pool_key),
        None,
    )
    if runner_pool is None:
        _, payload = client.request(
            "POST",
            "/api/v1/admin/runner-pools",
            headers=headers,
            payload={
                "key": runner_pool_key,
                "name": "Compose Web Runner Pool",
                "description": "Deterministic pool used by the full-stack Compose smoke.",
                "target_types": ["WEB"],
                "max_concurrency": int(_setting("RUNNER_MAX_SLOTS", "1")),
            },
            expected=(201,),
        )
        runner_pool = _object(payload, "POST /api/v1/admin/runner-pools")
    elif "WEB" not in runner_pool.get("target_types", []):
        raise SmokeError("configured Compose Runner Pool does not support WEB targets")
    runner_pool_id = str(runner_pool["id"])

    _, payload = client.request("GET", "/api/v1/projects", headers=headers)
    projects = _objects(payload, "GET /api/v1/projects")
    project = next((item for item in projects if item.get("key") == PROJECT_KEY), None)
    if project is None:
        _, payload = client.request(
            "POST",
            "/api/v1/projects",
            headers=headers,
            payload={
                "key": PROJECT_KEY,
                "name": "Platform Compose Smoke",
                "description": "Deterministic full-stack integration smoke resources.",
            },
            expected=(201,),
        )
        project = _object(payload, "POST /api/v1/projects")
    project_id = str(project["id"])

    targets_path = f"/api/v1/projects/{project_id}/targets"
    _, payload = client.request("GET", targets_path, headers=headers)
    targets = _objects(payload, f"GET {targets_path}")
    target = next((item for item in targets if item.get("key") == TARGET_KEY), None)
    if target is None:
        _, payload = client.request(
            "POST",
            targets_path,
            headers=headers,
            payload={
                "key": TARGET_KEY,
                "name": "Compose Chromium",
                "target_type": "WEB",
                "browser": "chromium",
                "runner_pool_id": runner_pool_id,
            },
            expected=(201,),
        )
        target = _object(payload, f"POST {targets_path}")
    elif target.get("runner_pool_id") != runner_pool_id:
        _, payload = client.request(
            "PATCH",
            f"{targets_path}/{target['id']}",
            headers=headers,
            payload={"runner_pool_id": runner_pool_id},
        )
        target = _object(payload, f"PATCH {targets_path}/{target['id']}")
    target_id = str(target["id"])

    environments_path = f"/api/v1/projects/{project_id}/targets/{target_id}/environments"
    _, payload = client.request("GET", environments_path, headers=headers)
    environments = _objects(payload, f"GET {environments_path}")
    environment = next(
        (item for item in environments if item.get("key") == ENVIRONMENT_KEY),
        None,
    )
    environment_payload = {
        "key": ENVIRONMENT_KEY,
        "name": "Compose Smoke",
        "web_config": {
            "base_url": target_url.rstrip("/"),
            "headless": True,
            "action_timeout_seconds": 10,
            "navigation_timeout_seconds": 15,
            "capture_trace": True,
        },
        "variables": [],
        "secret_bindings": [
            {
                "name": "SMOKE_USERNAME",
                "ref": "secret://compose-smoke/username",
            },
            {
                "name": "SMOKE_PASSWORD",
                "ref": "secret://compose-smoke/password",
            },
        ],
    }
    if environment is None:
        _, payload = client.request(
            "POST",
            environments_path,
            headers=headers,
            payload=environment_payload,
            expected=(201,),
        )
        environment = _object(payload, f"POST {environments_path}")
    else:
        web_config = _object(environment.get("web_config"), "existing smoke web_config")
        if web_config.get("base_url") != target_url.rstrip("/"):
            raise SmokeError(
                "existing Compose smoke environment points to a different target; "
                "use the original target URL or a fresh database"
            )
    environment_id = str(environment["id"])

    baseline = build_smoke_baseline()
    baseline_path = f"/api/v1/projects/{project_id}/baselines"
    _, payload = client.request(
        "POST",
        baseline_path,
        headers=headers,
        payload={
            "baseline": baseline.model_dump(mode="json", exclude_none=True),
            "digest": canonical_sha256(baseline),
        },
        expected=(200, 201),
    )
    baseline_response = _object(payload, f"POST {baseline_path}")
    baseline_id = str(baseline_response["baseline_id"])

    packages_path = f"/api/v1/projects/{project_id}/targets/{target_id}/automation-packages"
    package_digest = runner_source_digest()
    _, payload = client.request("GET", packages_path, headers=headers)
    packages = _objects(payload, f"GET {packages_path}")
    package = next(
        (
            item
            for item in packages
            if item.get("name") == PACKAGE_NAME and item.get("version") == PACKAGE_VERSION
        ),
        None,
    )
    if package is None:
        _, payload = client.request(
            "POST",
            packages_path,
            headers=headers,
            payload={
                "name": PACKAGE_NAME,
                "version": PACKAGE_VERSION,
                "digest": package_digest,
            },
            expected=(201,),
        )
        package = _object(payload, f"POST {packages_path}")
    elif package.get("digest") != package_digest:
        raise SmokeError("existing Compose smoke automation package has a different digest")

    return SmokeResources(
        project_id=project_id,
        target_id=target_id,
        environment_id=environment_id,
        baseline_id=baseline_id,
        package_id=str(package["id"]),
    )


def create_run(
    client: ApiClient,
    headers: dict[str, str],
    resources: SmokeResources,
    case_code: str,
) -> dict[str, Any]:
    _, payload = client.request(
        "POST",
        "/api/v1/runs",
        headers={
            **headers,
            "Idempotency-Key": f"compose-smoke-{uuid4().hex}",
        },
        payload={
            "project_id": resources.project_id,
            "target_id": resources.target_id,
            "environment_id": resources.environment_id,
            "baseline_id": resources.baseline_id,
            "automation_package_id": resources.package_id,
            "case_codes": [case_code],
        },
        expected=(201,),
    )
    return _object(payload, "POST /api/v1/runs")


def wait_for_terminal_run(
    client: ApiClient,
    headers: dict[str, str],
    run_id: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    previous_status = ""
    while time.monotonic() < deadline:
        _, payload = client.request("GET", f"/api/v1/runs/{run_id}", headers=headers)
        detail = _object(payload, f"GET /api/v1/runs/{run_id}")
        status = str(detail.get("status"))
        if status != previous_status:
            print(json.dumps({"run_id": run_id, "status": status}), flush=True)
            previous_status = status
        if status in TERMINAL_STATUSES:
            return detail
        time.sleep(1)
    raise SmokeError(f"run {run_id} did not reach a terminal status within {timeout_seconds}s")


def validate_run(
    client: ApiClient,
    headers: dict[str, str],
    detail: dict[str, Any],
    *,
    expected_status: str,
    secret_value: str,
) -> dict[str, Any]:
    run_id = str(detail["id"])
    serialized = json.dumps(detail, ensure_ascii=False)
    if secret_value and secret_value in serialized:
        raise SmokeError(f"run {run_id} response leaked a bound secret value")
    if detail.get("status") != expected_status:
        result = detail.get("result")
        raw_cases = result.get("case_results") if isinstance(result, dict) else None
        diagnostics = []
        if isinstance(raw_cases, list):
            for case in raw_cases[:3]:
                if not isinstance(case, dict):
                    continue
                diagnostics.append(
                    {
                        "case_code": str(case.get("case_code", ""))[:100],
                        "failure_category": str(case.get("failure_category", ""))[:100],
                        "error_message": str(case.get("error_message", ""))[:500],
                    }
                )
        diagnostic_text = json.dumps(diagnostics, ensure_ascii=False, separators=(",", ":"))
        raise SmokeError(
            f"run {run_id} ended as {detail.get('status')}, expected {expected_status}; "
            f"diagnostics={diagnostic_text}"
        )
    result = _object(detail.get("result"), f"run {run_id} result")
    isolation = _object(
        result.get("execution_isolation"),
        f"run {run_id} execution isolation evidence",
    )
    expected_isolation = {
        "mode": "CONTAINER",
        "executor_version": PACKAGE_VERSION,
        "dedicated_process": True,
        "credential_scope": "RUN_SECRETS_ONLY",
        "workspace_scope": "RUN_DIRECTORY",
        "read_only_root_filesystem": True,
        "network_policy": os.getenv("RUNNER_CONTAINER_NETWORK_POLICY", "ALLOWLIST"),
        "resource_limits_enforced": True,
        "memory_limit_bytes": int(os.getenv("RUNNER_CONTAINER_MEMORY_MIB", "1024")) * 1024 * 1024,
        "cpu_limit_millis": int(os.getenv("RUNNER_CONTAINER_CPU_MILLIS", "1000")),
        "pids_limit": int(os.getenv("RUNNER_CONTAINER_PIDS_LIMIT", "256")),
    }
    mismatches = {
        field: {"expected": expected, "actual": isolation.get(field)}
        for field, expected in expected_isolation.items()
        if isolation.get(field) != expected
    }
    if mismatches:
        raise SmokeError(f"run {run_id} isolation evidence mismatch: {mismatches}")
    runtime_image_id = isolation.get("runtime_image_id")
    if not isinstance(runtime_image_id, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", runtime_image_id
    ):
        raise SmokeError(f"run {run_id} has no immutable runtime image ID")

    _, payload = client.request(
        "GET",
        f"/api/v1/runs/{run_id}/events",
        headers=headers,
    )
    events = _objects(payload, f"GET /api/v1/runs/{run_id}/events")
    event_types = {str(event.get("event_type")) for event in events}
    required_events = {
        "run_created",
        "run_started",
        "case_started",
        "case_finished",
        "run_finished",
        "result_recorded",
    }
    missing_events = sorted(required_events - event_types)
    if missing_events:
        raise SmokeError(f"run {run_id} is missing timeline events: {missing_events}")
    sequences = [int(event["sequence"]) for event in events]
    if sequences != sorted(set(sequences)):
        raise SmokeError(f"run {run_id} event sequences are not unique and monotonic")

    artifacts = _objects(detail.get("artifacts"), f"run {run_id} artifacts")
    if not artifacts:
        raise SmokeError(f"run {run_id} did not persist any artifacts")
    artifact_kinds = {str(artifact.get("kind")) for artifact in artifacts}
    if expected_status == "FAILED":
        required_kinds = {"LOG", "SCREENSHOT", "TRACE"}
        missing_kinds = sorted(required_kinds - artifact_kinds)
        if missing_kinds:
            raise SmokeError(f"run {run_id} is missing artifact kinds: {missing_kinds}")

    verified_downloads = 0
    for artifact in artifacts:
        artifact_id = str(artifact["artifact_id"])
        _, payload = client.request(
            "GET",
            f"/api/v1/runs/{run_id}/artifacts/{artifact_id}/access",
            headers=headers,
        )
        access = _object(payload, "artifact access")
        url = access.get("url")
        if not isinstance(url, str) or not url:
            raise SmokeError(f"artifact {artifact_id} access response has no URL")
        content = client.download(url)
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        if digest != artifact.get("digest") or len(content) != artifact.get("size_bytes"):
            raise SmokeError(f"artifact {artifact_id} download metadata does not match")
        verified_downloads += 1

    return {
        "run_id": run_id,
        "status": expected_status,
        "events": len(events),
        "artifacts": len(artifacts),
        "verified_downloads": verified_downloads,
        "execution_isolation": isolation,
    }


def _docker_compose(*arguments: str) -> None:
    configured_docker = os.getenv("TESTOPS_DOCKER_EXECUTABLE", "").strip()
    docker = configured_docker or shutil.which("docker")
    if docker is None:
        raise SmokeError("docker is required for --exercise-recovery")
    subprocess.run(
        [docker, "compose", *arguments],
        cwd=ROOT,
        check=True,
    )


def create_success_run(
    client: ApiClient,
    headers: dict[str, str],
    resources: SmokeResources,
    *,
    exercise_recovery: bool,
) -> dict[str, Any]:
    if not exercise_recovery:
        return create_run(client, headers, resources, PASS_CASE_CODE)

    print("stopping Outbox and Worker to verify durable recovery", flush=True)
    _docker_compose("stop", "outbox", "worker")
    try:
        run = create_run(client, headers, resources, PASS_CASE_CODE)
    finally:
        _docker_compose("start", "worker", "outbox")
    return run


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    if args.timeout_seconds <= 0:
        raise SmokeError("--timeout-seconds must be greater than zero")
    client = ApiClient(args.base_url)
    wait_until_ready(client, args.timeout_seconds)
    wait_for_http_url(
        args.target_health_url,
        args.timeout_seconds,
        label="Compose smoke target",
    )
    headers = authenticate(
        client,
        bootstrap_token=args.bootstrap_token,
        username=args.admin_username,
        password=args.admin_password,
    )
    resources = ensure_resources(client, headers, target_url=args.target_url)

    success = create_success_run(
        client,
        headers,
        resources,
        exercise_recovery=args.exercise_recovery,
    )
    success_detail = wait_for_terminal_run(
        client,
        headers,
        str(success["id"]),
        timeout_seconds=args.timeout_seconds,
    )
    success_summary = validate_run(
        client,
        headers,
        success_detail,
        expected_status="PASSED",
        secret_value=args.smoke_password,
    )

    failure = create_run(client, headers, resources, FAIL_CASE_CODE)
    failure_detail = wait_for_terminal_run(
        client,
        headers,
        str(failure["id"]),
        timeout_seconds=args.timeout_seconds,
    )
    failure_summary = validate_run(
        client,
        headers,
        failure_detail,
        expected_status="FAILED",
        secret_value=args.smoke_password,
    )

    return {
        "status": "passed",
        "recovery_exercised": args.exercise_recovery,
        "project_id": resources.project_id,
        "success_run": success_summary,
        "artifact_run": failure_summary,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smoke-compose")
    parser.add_argument(
        "--base-url",
        default=_setting("TESTOPS_SMOKE_BASE_URL", "http://127.0.0.1:8080"),
        help="frontend/reverse-proxy URL (default: http://127.0.0.1:8080)",
    )
    parser.add_argument(
        "--target-url",
        default=_setting("TESTOPS_SMOKE_TARGET_URL", "http://smoke-target:8080"),
        help="URL reachable from the Worker container",
    )
    parser.add_argument(
        "--target-health-url",
        default=_setting(
            "TESTOPS_SMOKE_TARGET_HEALTH_URL",
            "http://127.0.0.1:18080/healthz",
        ),
        help="smoke target health URL reachable from the host",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=180,
        help="stack and per-Run timeout (default: 180)",
    )
    parser.add_argument(
        "--exercise-recovery",
        action="store_true",
        help="queue a Run while Outbox and Worker are stopped, then restart them",
    )
    parser.set_defaults(
        bootstrap_token=_setting(
            "BOOTSTRAP_ADMIN_TOKEN",
            "change-me-one-time-bootstrap-token",
        ),
        admin_username=_setting("TESTOPS_SMOKE_ADMIN_USERNAME", "smoke-admin"),
        admin_password=_setting(
            "TESTOPS_SMOKE_ADMIN_PASSWORD",
            "testops-smoke-admin-password",
        ),
        smoke_password="smoke-password",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        summary = run_smoke(args)
    except (SmokeError, subprocess.CalledProcessError) as exc:
        print(f"compose smoke failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
