from __future__ import annotations

import io
import json
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from scripts.check_supply_chain_keyring_fleet import (
    FleetInstance,
    FleetPreflightError,
    FleetReport,
    InstanceReadiness,
    _RejectRedirects,
    audit_fleet,
    evaluate_fleet,
    fetch_instance_readiness,
    fleet_exit_code,
    load_bearer_token,
    main,
    parse_instances,
    parse_readiness_response,
    safe_fleet_report,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
TOKEN = "short-lived-admin-session-token"


def readiness(
    name: str,
    *,
    status: str = "READY",
    digest: str = DIGEST_A,
) -> InstanceReadiness:
    return InstanceReadiness(
        name=name,
        status=status,
        configuration_digest=digest,
        generated_at="2026-08-24T12:00:00+00:00",
        configured_key_count=2,
        accepting_key_count=2,
        overlap_ready=True,
        legacy_hmac_enabled=False,
        warnings=() if status == "READY" else ("NO_OVERLAPPING_ACTIVE_KEYS",),
    )


def test_instances_require_unique_direct_origins_and_https_by_default() -> None:
    instances = parse_instances(
        ["api-a=https://api-a.internal", "api-b=https://api-b.internal"],
        allow_http=False,
    )
    assert instances == (
        FleetInstance(name="api-a", origin="https://api-a.internal"),
        FleetInstance(name="api-b", origin="https://api-b.internal"),
    )

    with pytest.raises(FleetPreflightError, match="at least two"):
        parse_instances(["api-a=https://api-a.internal"], allow_http=False)
    with pytest.raises(FleetPreflightError, match="explicit --allow-http"):
        parse_instances(
            ["api-a=http://127.0.0.1:8000", "api-b=http://127.0.0.1:8001"],
            allow_http=False,
        )
    with pytest.raises(FleetPreflightError, match="restricted to loopback"):
        parse_instances(
            ["api-a=http://api-a.internal", "api-b=http://api-b.internal"],
            allow_http=True,
        )
    with pytest.raises(FleetPreflightError, match="origins must be unique"):
        parse_instances(
            ["api-a=https://API.internal", "api-b=https://api.internal:443/"],
            allow_http=False,
        )


def test_token_loader_is_bounded_and_rejects_whitespace() -> None:
    with tempfile.TemporaryDirectory() as directory:
        token_file = Path(directory) / "admin.token"
        token_file.write_text(TOKEN + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        assert load_bearer_token(token_file) == TOKEN

        token_file.write_text("two tokens", encoding="utf-8")
        with pytest.raises(FleetPreflightError, match="one non-whitespace"):
            load_bearer_token(token_file)


def test_readiness_parser_keeps_only_safe_aggregate_contract() -> None:
    content = json.dumps(
        {
            "status": "READY",
            "configuration_digest": DIGEST_A,
            "generated_at": "2026-08-24T12:00:00+00:00",
            "configured_key_count": 2,
            "accepting_key_count": 2,
            "overlap_ready": True,
            "legacy_hmac_enabled": False,
            "warnings": [],
            "keys": [
                {
                    "credential_id": "must-not-be-copied",
                    "workload_identity": "spiffe://must-not-be-copied",
                    "key_fingerprint": "sha256:must-not-be-copied",
                }
            ],
        }
    ).encode()
    snapshot = parse_readiness_response(
        FleetInstance(name="api-a", origin="https://api-a.internal"),
        content,
    )
    rendered = json.dumps(safe_fleet_report(evaluate_fleet([snapshot, readiness("api-b")])))

    assert snapshot.configuration_digest == DIGEST_A
    assert "must-not-be-copied" not in rendered

    unsafe = json.loads(content)
    unsafe["warnings"] = ["credential=must-not-be-copied"]
    with pytest.raises(FleetPreflightError, match="readiness contract"):
        parse_readiness_response(
            FleetInstance(name="api-a", origin="https://api-a.internal"),
            json.dumps(unsafe).encode(),
        )

    invalid_time = json.loads(content)
    invalid_time["generated_at"] = "not-a-timestamp"
    with pytest.raises(FleetPreflightError, match="readiness contract"):
        parse_readiness_response(
            FleetInstance(name="api-a", origin="https://api-a.internal"),
            json.dumps(invalid_time).encode(),
        )


def test_fetch_uses_bearer_auth_no_cache_and_a_bounded_response() -> None:
    payload = {
        "status": "READY",
        "configuration_digest": DIGEST_A,
        "generated_at": "2026-08-24T12:00:00+00:00",
        "configured_key_count": 2,
        "accepting_key_count": 2,
        "overlap_ready": True,
        "legacy_hmac_enabled": False,
        "warnings": [],
    }
    response = MagicMock()
    response.status = 200
    response.headers = {"Content-Length": str(len(json.dumps(payload).encode()))}
    response.read.return_value = json.dumps(payload).encode()
    response.__enter__.return_value = response
    opener = MagicMock()
    opener.open.return_value = response
    instance = FleetInstance(name="api-a", origin="https://api-a.internal")

    with patch(
        "scripts.check_supply_chain_keyring_fleet.build_opener", return_value=opener
    ) as build:
        snapshot = fetch_instance_readiness(
            instance,
            token=TOKEN,
            timeout_seconds=7,
        )

    assert snapshot.status == "READY"
    assert isinstance(build.call_args.args[0], _RejectRedirects)
    request = opener.open.call_args.args[0]
    assert request.full_url.endswith("/api/v1/admin/supply-chain/verifier-keys")
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert request.get_header("Cache-control") == "no-cache"
    assert opener.open.call_args.kwargs["timeout"] == 7


def test_fleet_requires_ready_instances_one_digest_and_optional_approved_digest() -> None:
    ready = evaluate_fleet([readiness("api-b"), readiness("api-a")])
    assert ready.status == "READY"
    assert ready.configuration_digest == DIGEST_A
    assert [item.name for item in ready.instances] == ["api-a", "api-b"]
    assert fleet_exit_code(ready.status) == 0

    warning = evaluate_fleet([readiness("api-a"), readiness("api-b", status="WARNING")])
    assert warning.status == "NOT_READY"
    assert fleet_exit_code(warning.status) == 2
    assert (
        evaluate_fleet(
            [readiness("api-a"), readiness("api-b", status="WARNING")],
            allow_warning=True,
        ).status
        == "READY"
    )

    drift = evaluate_fleet([readiness("api-a"), readiness("api-b", digest=DIGEST_B)])
    assert drift.status == "DRIFT"
    assert drift.configuration_digest is None
    assert fleet_exit_code(drift.status) == 3

    unexpected = evaluate_fleet(
        [readiness("api-a"), readiness("api-b")],
        expected_digest=DIGEST_B,
    )
    assert unexpected.status == "DRIFT"
    assert unexpected.configuration_digest == DIGEST_A


def test_audit_passes_token_to_each_fetch_without_adding_it_to_report() -> None:
    calls: list[tuple[str, str, float, Path | None]] = []

    def fetch(
        instance: FleetInstance,
        *,
        token: str,
        timeout_seconds: float,
        ca_file: Path | None,
    ) -> InstanceReadiness:
        calls.append((instance.name, token, timeout_seconds, ca_file))
        return readiness(instance.name)

    report = audit_fleet(
        (
            FleetInstance(name="api-a", origin="https://api-a.internal"),
            FleetInstance(name="api-b", origin="https://api-b.internal"),
        ),
        token=TOKEN,
        timeout_seconds=5,
        ca_file=Path("approved-ca.pem"),
        fetch=fetch,
    )

    assert report.status == "READY"
    assert calls == [
        ("api-a", TOKEN, 5, Path("approved-ca.pem")),
        ("api-b", TOKEN, 5, Path("approved-ca.pem")),
    ]
    assert TOKEN not in json.dumps(safe_fleet_report(report))


def test_cli_outputs_safe_json_and_returns_generic_transport_failure() -> None:
    report = FleetReport(
        status="READY",
        configuration_digest=DIGEST_A,
        expected_digest=None,
        instances=(readiness("api-a"), readiness("api-b")),
    )
    with tempfile.TemporaryDirectory() as directory:
        token_file = Path(directory) / "admin.token"
        token_file.write_text(TOKEN, encoding="utf-8")
        token_file.chmod(0o600)
        arguments = [
            "--instance",
            "api-a=https://api-a.internal",
            "--instance",
            "api-b=https://api-b.internal",
            "--token-file",
            str(token_file),
        ]
        stdout = io.StringIO()
        with (
            patch(
                "scripts.check_supply_chain_keyring_fleet.audit_fleet",
                return_value=report,
            ),
            redirect_stdout(stdout),
        ):
            assert main(arguments) == 0

        output = stdout.getvalue()
        assert json.loads(output)["status"] == "READY"
        assert TOKEN not in output

        stderr = io.StringIO()
        with (
            patch(
                "scripts.check_supply_chain_keyring_fleet.audit_fleet",
                side_effect=FleetPreflightError("secret-bearing error", instance="api-b"),
            ),
            redirect_stderr(stderr),
        ):
            assert main(arguments) == 4

    error_output = stderr.getvalue()
    assert json.loads(error_output) == {
        "error": "fleet preflight failed",
        "instance": "api-b",
        "status": "INVALID",
    }
    assert "secret-bearing error" not in error_output
    assert TOKEN not in error_output


def test_redirect_handler_refuses_redirects() -> None:
    assert (
        _RejectRedirects().redirect_request(None, None, 302, "Found", {}, "https://other") is None
    )
