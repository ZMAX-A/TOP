"""Build a deterministic seven-case login Run Snapshot example."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid5

ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "packages/contracts/src",
    "packages/migrations/src",
    "runners/web_playwright/src",
):
    sys.path.insert(0, str(ROOT / relative))

from testops.contracts import (  # noqa: E402
    AutomationPackageRef,
    CaseBaseline,
    CaseBaselineRef,
    RunSnapshot,
    RuntimeVariable,
    SecretBinding,
    TargetType,
    WebRunConfig,
)
from testops.migrations.legacy_excel import canonical_json_bytes  # noqa: E402
from testops.runners.web.engine import WebCaseEngine  # noqa: E402

ID_NAMESPACE = UUID("dddf9288-5a98-5d99-8cb6-15dac690ea87")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="build-login-run-snapshot")
    parser.add_argument(
        "--baseline",
        default=str(ROOT / "baselines/yanjia-ai-web/case-v1.0.1"),
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "examples/jobs/yanjia-login-smoke.json"),
    )
    parser.add_argument("--base-url", default="https://example.invalid/login")
    parser.add_argument(
        "--run-id",
        default="9feebdb4-96e4-5b31-bbaa-2c51acb5cf9c",
    )
    parser.add_argument("--created-at", default="2026-08-11T16:00:00+08:00")
    return parser


def _runner_digest() -> str:
    source_root = ROOT / "runners/web_playwright/src"
    digest = hashlib.sha256()
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(source_root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _config_digest(
    web_config: WebRunConfig,
    variables: tuple[RuntimeVariable, ...],
    secret_bindings: tuple[SecretBinding, ...],
) -> str:
    payload = canonical_json_bytes(
        {
            "web_config": web_config.model_dump(mode="json"),
            "variables": [item.model_dump(mode="json") for item in variables],
            "secret_bindings": [item.model_dump(mode="json") for item in secret_bindings],
        }
    )
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def main() -> int:
    args = _parser().parse_args()
    baseline_directory = Path(args.baseline)
    baseline_bytes = (baseline_directory / "case-baseline.json").read_bytes()
    manifest = json.loads((baseline_directory / "manifest.json").read_text("utf-8"))
    actual_digest = f"sha256:{hashlib.sha256(baseline_bytes).hexdigest()}"
    if actual_digest != manifest["baseline"]["digest"]:
        raise ValueError("baseline file does not match its manifest")
    baseline = CaseBaseline.model_validate_json(baseline_bytes)
    cases = tuple(case for case in baseline.cases if case.module_key == "login" and case.enabled)
    if len(cases) != 7:
        raise ValueError(f"expected 7 enabled login cases, found {len(cases)}")
    engine = WebCaseEngine()
    for case in cases:
        engine.validate_case(case)

    web_config = WebRunConfig(base_url=args.base_url, capture_trace=True)
    variables = (RuntimeVariable(name="STORE_NAME", value=""),)
    secret_bindings = (
        SecretBinding(
            name="TEST_USERNAME",
            ref="secret://yanjia/staging/test-username",
        ),
        SecretBinding(
            name="TEST_PASSWORD",
            ref="secret://yanjia/staging/test-password",
        ),
    )
    snapshot = RunSnapshot(
        run_id=UUID(args.run_id),
        project_id=uuid5(ID_NAMESPACE, "project:yanjia-ai"),
        target_id=uuid5(ID_NAMESPACE, "target:yanjia-ai:web"),
        target_type=TargetType.WEB,
        environment_id=uuid5(ID_NAMESPACE, f"environment:{args.base_url}"),
        case_baseline=CaseBaselineRef(
            baseline_id=baseline.baseline_id,
            version=baseline.version,
            digest=actual_digest,
            case_count=len(baseline.cases),
        ),
        automation_package=AutomationPackageRef(
            name="yanjia-web",
            version="0.1.0",
            digest=_runner_digest(),
        ),
        cases=cases,
        browser="chromium",
        web_config=web_config,
        config_hash=_config_digest(web_config, variables, secret_bindings),
        variables=variables,
        secret_bindings=secret_bindings,
        created_by=uuid5(ID_NAMESPACE, "actor:local-developer"),
        created_at=datetime.fromisoformat(args.created_at),
    )
    output = Path(args.output)
    payload = canonical_json_bytes(snapshot)
    if output.exists() and output.read_bytes() != payload:
        raise ValueError("refusing to overwrite a different Run Snapshot; use a new run_id")
    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.exists():
        output.write_bytes(payload)
    print(
        json.dumps(
            {
                "run_id": str(snapshot.run_id),
                "baseline_version": snapshot.case_baseline.version,
                "case_count": len(snapshot.cases),
                "output": str(output.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
