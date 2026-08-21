from __future__ import annotations

import base64
import io
import json
import tempfile
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts.submit_supply_chain_verification import (
    VerificationClientError,
    _RejectRedirects,
    build_signed_request,
    load_private_key,
    load_report,
    main,
    submit,
)

from testops.api.config import Settings, SupplyChainVerifierPublicKey
from testops.api.supply_chain_auth import SupplyChainVerifierAuth

PROJECT_ID = UUID("10000000-0000-4000-8000-000000000001")
TARGET_ID = UUID("20000000-0000-4000-8000-000000000002")
PACKAGE_ID = UUID("30000000-0000-4000-8000-000000000003")
CREDENTIAL_ID = "trusted-verifier/2026-08-ed25519"
WORKLOAD_IDENTITY = "https://github.com/example/testops/.github/workflows/release.yml"
PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))


def report() -> dict[str, object]:
    return {
        "outcome": "VERIFIED",
        "policy_version": "testops-supply-chain-v1",
        "verifier": "trusted-verifier",
        "image_digest": "sha256:" + "a" * 64,
        "signature_bundle_digest": "sha256:" + "b" * 64,
        "provenance_digest": "sha256:" + "c" * 64,
        "sbom_digest": "sha256:" + "d" * 64,
        "signature_verified": True,
        "transparency_log_verified": True,
        "provenance_verified": True,
        "sbom_verified": True,
        "certificate_issuer": "https://token.actions.githubusercontent.com",
        "certificate_identity": WORKLOAD_IDENTITY,
        "builder_id": "https://github.com/actions/runner",
        "source_repository": "https://github.com/example/testops",
        "source_revision": "e" * 40,
        "reason": None,
    }


def test_client_request_is_accepted_by_server_envelope_authentication() -> None:
    path = (
        f"/api/v1/projects/{PROJECT_ID}/targets/{TARGET_ID}/automation-packages/"
        f"{PACKAGE_ID}/supply-chain-verifications"
    )
    app = FastAPI()
    public_key = PRIVATE_KEY.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    app.state.settings = Settings(
        supply_chain_verifier_public_keys=(
            SupplyChainVerifierPublicKey(
                credential_id=CREDENTIAL_ID,
                verifier="trusted-verifier",
                workload_identity=WORKLOAD_IDENTITY,
                public_key=public_key,
            ),
        )
    )

    @app.post(path)
    async def verify(principal: SupplyChainVerifierAuth) -> dict[str, str | None]:
        return {
            "algorithm": principal.signature_algorithm,
            "credential_id": principal.credential_id,
            "workload_identity": principal.workload_identity,
            "request_digest": principal.request_digest,
        }

    signed = build_signed_request(
        base_url="http://127.0.0.1",
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        package_id=PACKAGE_ID,
        report=report(),
        credential_id=CREDENTIAL_ID,
        workload_identity=WORKLOAD_IDENTITY,
        private_key=PRIVATE_KEY,
        allow_http=True,
        created=str(int(datetime.now(UTC).timestamp())),
        nonce="40000000-0000-4000-8000-000000000004",
    )
    response = TestClient(app).post(path, content=signed.body, headers=signed.headers)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "algorithm": "ED25519",
        "credential_id": CREDENTIAL_ID,
        "workload_identity": WORKLOAD_IDENTITY,
        "request_digest": signed.request_digest,
    }
    assert signed.path == path
    assert signed.headers["X-TestOps-Envelope-Signature"].count(".") == 2


def test_client_refuses_http_identity_mismatch_and_noncanonical_nonce() -> None:
    arguments = {
        "base_url": "http://127.0.0.1",
        "project_id": PROJECT_ID,
        "target_id": TARGET_ID,
        "package_id": PACKAGE_ID,
        "report": report(),
        "credential_id": CREDENTIAL_ID,
        "workload_identity": WORKLOAD_IDENTITY,
        "private_key": PRIVATE_KEY,
    }
    with pytest.raises(VerificationClientError, match="explicit --allow-http"):
        build_signed_request(**arguments)

    with pytest.raises(VerificationClientError, match="restricted to loopback"):
        build_signed_request(
            **{**arguments, "base_url": "http://testops.example.invalid", "allow_http": True}
        )

    mismatched = report()
    mismatched["verifier"] = "another-verifier"
    with pytest.raises(VerificationClientError, match="verifier must match"):
        build_signed_request(**{**arguments, "report": mismatched, "allow_http": True})

    with pytest.raises(VerificationClientError, match="canonical lowercase UUID"):
        build_signed_request(
            **arguments,
            allow_http=True,
            nonce="4A000000-0000-4000-8000-00000000000B",
        )


def test_private_key_loader_accepts_pkcs8_pem_and_raw_base64url() -> None:
    pem = PRIVATE_KEY.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    raw = PRIVATE_KEY.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        pem_path = root / "key.pem"
        raw_path = root / "key.b64url"
        pem_path.write_bytes(pem)
        raw_path.write_bytes(encoded)
        pem_path.chmod(0o600)
        raw_path.chmod(0o600)

        loaded_pem = load_private_key(pem_path)
        loaded_raw = load_private_key(raw_path)

    expected_public_key = PRIVATE_KEY.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    for loaded in (loaded_pem, loaded_raw):
        assert (
            loaded.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            == expected_public_key
        )


def test_report_loader_rejects_incomplete_verification_contract() -> None:
    with tempfile.TemporaryDirectory() as directory:
        report_path = Path(directory) / "report.json"
        report_path.write_text('{"verifier":"trusted-verifier"}', encoding="utf-8")

        with pytest.raises(VerificationClientError, match="verification contract"):
            load_report(report_path)


def test_submit_uses_no_redirect_transport_and_bounded_json_response() -> None:
    signed = build_signed_request(
        base_url="https://testops.example.invalid",
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        package_id=PACKAGE_ID,
        report=report(),
        credential_id=CREDENTIAL_ID,
        workload_identity=WORKLOAD_IDENTITY,
        private_key=PRIVATE_KEY,
    )
    response = MagicMock()
    response.status = 201
    response.read.return_value = b'{"id":"verification-id"}'
    response.__enter__.return_value = response
    opener = MagicMock()
    opener.open.return_value = response

    with patch(
        "scripts.submit_supply_chain_verification.build_opener",
        return_value=opener,
    ) as build:
        payload = submit(signed, timeout_seconds=30)

    assert payload == {"id": "verification-id"}
    assert isinstance(build.call_args.args[0], _RejectRedirects)
    request = opener.open.call_args.args[0]
    assert request.full_url == signed.url
    assert request.data == signed.body
    assert opener.open.call_args.kwargs["timeout"] == 30


def test_dry_run_does_not_print_signature_or_private_key_material() -> None:
    pem = PRIVATE_KEY.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        report_path = root / "report.json"
        key_path = root / "key.pem"
        report_path.write_text(json.dumps(report()), encoding="utf-8")
        key_path.write_bytes(pem)
        key_path.chmod(0o600)
        arguments = [
            "submit-supply-chain-verification",
            "--base-url",
            "https://testops.example.invalid",
            "--project-id",
            str(PROJECT_ID),
            "--target-id",
            str(TARGET_ID),
            "--package-id",
            str(PACKAGE_ID),
            "--report-file",
            str(report_path),
            "--credential-id",
            CREDENTIAL_ID,
            "--workload-identity",
            WORKLOAD_IDENTITY,
            "--private-key-file",
            str(key_path),
            "--dry-run",
        ]
        stdout = io.StringIO()
        with patch("sys.argv", arguments), redirect_stdout(stdout):
            assert main() == 0

    output = stdout.getvalue()
    assert json.loads(output)["status"] == "validated"
    assert "X-TestOps-Envelope-Signature" not in output
    assert "PRIVATE KEY" not in output
