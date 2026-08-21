"""Sign and submit one supply-chain verification report from trusted CI."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/contracts/src"))
sys.path.insert(0, str(ROOT / "apps/api/src"))

from testops.api.schemas import AutomationPackageSupplyChainVerificationCreate  # noqa: E402
from testops.contracts.supply_chain_envelope import (  # noqa: E402
    base64url_encode,
    supply_chain_detached_jws_signing_input,
    supply_chain_jws_protected_header,
)

MAX_REPORT_BYTES = 1024 * 1024
MAX_PRIVATE_KEY_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
VERIFIER_CREDENTIAL_ID_PATTERN = re.compile(
    r"^(?P<verifier>[a-z0-9][a-z0-9._-]{2,63})/"
    r"(?P<key_id>[A-Za-z0-9][A-Za-z0-9._-]{0,63})$"
)


class VerificationClientError(RuntimeError):
    """Safe operator-facing validation or transport failure."""


@dataclass(frozen=True, slots=True)
class SignedVerificationRequest:
    url: str
    path: str
    body: bytes
    headers: dict[str, str]
    request_digest: str
    created: str
    nonce: str
    key_fingerprint: str


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def _read_bounded_file(path: Path, *, limit: int, label: str) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise VerificationClientError(f"cannot inspect {label} file") from exc
    if size <= 0 or size > limit:
        raise VerificationClientError(f"{label} file size must be between 1 and {limit} bytes")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise VerificationClientError(f"cannot read {label} file") from exc
    if len(content) != size:
        raise VerificationClientError(f"{label} file changed while being read")
    return content


def load_report(path: Path) -> dict[str, Any]:
    content = _read_bounded_file(path, limit=MAX_REPORT_BYTES, label="report")
    try:
        report = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationClientError("report file must contain UTF-8 JSON") from exc
    if not isinstance(report, dict):
        raise VerificationClientError("report JSON must be an object")
    return validate_report(report)


def validate_report(report: dict[str, Any]) -> dict[str, Any]:
    try:
        validated = AutomationPackageSupplyChainVerificationCreate.model_validate(report)
    except ValidationError as exc:
        raise VerificationClientError(
            "report JSON does not match the supply-chain verification contract"
        ) from exc
    return validated.model_dump(mode="json")


def _decode_raw_private_key(content: bytes) -> bytes:
    try:
        encoded = content.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise VerificationClientError("private key must be PKCS8 PEM or raw base64url") from exc
    if not encoded or "=" in encoded or re.fullmatch(r"[A-Za-z0-9_-]+", encoded) is None:
        raise VerificationClientError("raw private key must use unpadded base64url")
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (binascii.Error, ValueError) as exc:
        raise VerificationClientError("raw private key must use unpadded base64url") from exc
    if len(decoded) != 32 or base64url_encode(decoded) != encoded:
        raise VerificationClientError("raw Ed25519 private key must contain exactly 32 bytes")
    return decoded


def load_private_key(path: Path) -> Ed25519PrivateKey:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise VerificationClientError("cannot inspect private key file") from exc
    if os.name != "nt" and mode & 0o077:
        raise VerificationClientError("private key file must not be readable by group or others")
    content = _read_bounded_file(path, limit=MAX_PRIVATE_KEY_BYTES, label="private key")
    if content.lstrip().startswith(b"-----BEGIN"):
        try:
            key = serialization.load_pem_private_key(content, password=None)
        except (TypeError, ValueError) as exc:
            raise VerificationClientError(
                "private key must be an unencrypted PKCS8 Ed25519 key"
            ) from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise VerificationClientError("private key must be Ed25519")
        return key
    try:
        return Ed25519PrivateKey.from_private_bytes(_decode_raw_private_key(content))
    except ValueError as exc:
        raise VerificationClientError("private key must be a valid Ed25519 key") from exc


def _validate_endpoint(base_url: str, *, allow_http: bool) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise VerificationClientError("base URL must be an absolute HTTP or HTTPS origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise VerificationClientError("base URL must not contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise VerificationClientError("base URL must not contain a path prefix")
    if parsed.scheme != "https":
        if not allow_http:
            raise VerificationClientError(
                "HTTP requires the explicit --allow-http development switch"
            )
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise VerificationClientError("the --allow-http switch is restricted to loopback hosts")
    return f"{parsed.scheme}://{parsed.netloc}"


def _validate_identity(credential_id: str, workload_identity: str) -> str:
    match = VERIFIER_CREDENTIAL_ID_PATTERN.fullmatch(credential_id)
    if match is None:
        raise VerificationClientError("credential id must be '<verifier>/<key-id>'")
    identity = urlsplit(workload_identity)
    if identity.scheme not in {"https", "spiffe"} or not identity.netloc:
        raise VerificationClientError("workload identity must be an HTTPS or SPIFFE URI")
    if len(workload_identity) > 512:
        raise VerificationClientError("workload identity cannot exceed 512 characters")
    return match.group("verifier")


def build_signed_request(
    *,
    base_url: str,
    project_id: UUID,
    target_id: UUID,
    package_id: UUID,
    report: dict[str, Any],
    credential_id: str,
    workload_identity: str,
    private_key: Ed25519PrivateKey,
    allow_http: bool = False,
    created: str | None = None,
    nonce: str | None = None,
) -> SignedVerificationRequest:
    origin = _validate_endpoint(base_url, allow_http=allow_http)
    verifier = _validate_identity(credential_id, workload_identity)
    report = validate_report(report)
    if report.get("verifier") != verifier:
        raise VerificationClientError("report verifier must match the credential id prefix")
    path = (
        f"/api/v1/projects/{project_id}/targets/{target_id}/automation-packages/"
        f"{package_id}/supply-chain-verifications"
    )
    body = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) > MAX_REPORT_BYTES:
        raise VerificationClientError("canonical report body exceeds 1 MiB")
    created_value = created or str(int(datetime.now(UTC).timestamp()))
    try:
        if str(int(created_value)) != created_value:
            raise ValueError
    except ValueError as exc:
        raise VerificationClientError("created must be canonical Unix seconds") from exc
    nonce_value = nonce or str(uuid4())
    try:
        if str(UUID(nonce_value)) != nonce_value:
            raise ValueError
    except ValueError as exc:
        raise VerificationClientError("nonce must be a canonical lowercase UUID") from exc
    request_digest = "sha256:" + hashlib.sha256(body).hexdigest()
    signing_input = supply_chain_detached_jws_signing_input(
        method="POST",
        path=path,
        created=created_value,
        nonce=nonce_value,
        request_digest=request_digest,
        credential_id=credential_id,
        workload_identity=workload_identity,
    )
    protected = base64url_encode(supply_chain_jws_protected_header(credential_id))
    signature = base64url_encode(private_key.sign(signing_input))
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return SignedVerificationRequest(
        url=origin + path,
        path=path,
        body=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-TestOps-Verifier-Key-Id": credential_id,
            "X-TestOps-Envelope-Created": created_value,
            "X-TestOps-Envelope-Nonce": nonce_value,
            "X-TestOps-Envelope-Signature": f"{protected}..{signature}",
        },
        request_digest=request_digest,
        created=created_value,
        nonce=nonce_value,
        key_fingerprint="sha256:" + hashlib.sha256(public_key).hexdigest(),
    )


def submit(request: SignedVerificationRequest, *, timeout_seconds: float) -> dict[str, Any]:
    if timeout_seconds <= 0 or timeout_seconds > 120:
        raise VerificationClientError("timeout must be greater than zero and at most 120 seconds")
    http_request = Request(
        request.url,
        data=request.body,
        headers=request.headers,
        method="POST",
    )
    opener = build_opener(_RejectRedirects())
    try:
        with opener.open(http_request, timeout=timeout_seconds) as response:
            content = response.read(MAX_RESPONSE_BYTES + 1)
            status = response.status
    except HTTPError as exc:
        detail = exc.read(MAX_RESPONSE_BYTES + 1)
        try:
            message = json.loads(detail).get("detail", "request rejected")
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            message = "request rejected"
        raise VerificationClientError(f"API returned HTTP {exc.code}: {message}") from exc
    except URLError as exc:
        raise VerificationClientError("cannot reach the TestOps API") from exc
    if len(content) > MAX_RESPONSE_BYTES:
        raise VerificationClientError("API response exceeds 1 MiB")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationClientError("API response must contain JSON") from exc
    if status not in {200, 201} or not isinstance(payload, dict):
        raise VerificationClientError(f"unexpected API response status {status}")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="submit-supply-chain-verification")
    parser.add_argument("--base-url", default=os.getenv("TESTOPS_BASE_URL"), required=False)
    parser.add_argument("--project-id", required=True, type=UUID)
    parser.add_argument("--target-id", required=True, type=UUID)
    parser.add_argument("--package-id", required=True, type=UUID)
    parser.add_argument("--report-file", required=True, type=Path)
    parser.add_argument(
        "--credential-id",
        default=os.getenv("TESTOPS_SUPPLY_CHAIN_CREDENTIAL_ID"),
    )
    parser.add_argument(
        "--workload-identity",
        default=os.getenv("TESTOPS_SUPPLY_CHAIN_WORKLOAD_IDENTITY"),
    )
    parser.add_argument(
        "--private-key-file",
        type=Path,
        default=(
            Path(os.environ["TESTOPS_SUPPLY_CHAIN_PRIVATE_KEY_FILE"])
            if os.getenv("TESTOPS_SUPPLY_CHAIN_PRIVATE_KEY_FILE")
            else None
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    for name in ("base_url", "credential_id", "workload_identity", "private_key_file"):
        if getattr(args, name) is None:
            raise VerificationClientError(f"--{name.replace('_', '-')} is required")
    report = load_report(args.report_file)
    private_key = load_private_key(args.private_key_file)
    signed = build_signed_request(
        base_url=args.base_url,
        project_id=args.project_id,
        target_id=args.target_id,
        package_id=args.package_id,
        report=report,
        credential_id=args.credential_id,
        workload_identity=args.workload_identity,
        private_key=private_key,
        allow_http=args.allow_http,
    )
    output: dict[str, Any] = {
        "status": "validated" if args.dry_run else "submitted",
        "endpoint": signed.url,
        "credential_id": args.credential_id,
        "workload_identity": args.workload_identity,
        "key_fingerprint": signed.key_fingerprint,
        "request_digest": signed.request_digest,
        "created": signed.created,
        "nonce": signed.nonce,
    }
    if not args.dry_run:
        output["verification"] = submit(signed, timeout_seconds=args.timeout_seconds)
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VerificationClientError as exc:
        print(f"supply-chain verification submission failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
