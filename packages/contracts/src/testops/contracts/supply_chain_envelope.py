"""Stable byte-level contract for authenticated supply-chain reports."""

from __future__ import annotations

import base64
import json

LEGACY_ENVELOPE_PROFILE = "testops-supply-chain-envelope-v1"
ASYMMETRIC_ENVELOPE_PROFILE = "testops-supply-chain-envelope-v2"
JWS_TYPE = "testops-supply-chain-envelope+jws"


def base64url_encode(value: bytes) -> str:
    """Return canonical unpadded base64url."""

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def supply_chain_envelope_signature_base(
    *,
    method: str,
    path: str,
    created: str,
    nonce: str,
    request_digest: str,
) -> bytes:
    """Build the legacy v1 HMAC input retained for controlled migration."""

    return "\n".join(
        (
            LEGACY_ENVELOPE_PROFILE,
            method.upper(),
            path,
            created,
            nonce,
            request_digest,
        )
    ).encode("utf-8")


def supply_chain_asymmetric_envelope_payload(
    *,
    method: str,
    path: str,
    created: str,
    nonce: str,
    request_digest: str,
    credential_id: str,
    workload_identity: str,
) -> bytes:
    """Build the v2 payload bound to the key id and workload identity."""

    return "\n".join(
        (
            ASYMMETRIC_ENVELOPE_PROFILE,
            method.upper(),
            path,
            created,
            nonce,
            request_digest,
            credential_id,
            workload_identity,
        )
    ).encode("utf-8")


def supply_chain_jws_protected_header(credential_id: str) -> bytes:
    """Serialize the exact protected JWS header accepted by the API."""

    return json.dumps(
        {
            "alg": "EdDSA",
            "kid": credential_id,
            "typ": JWS_TYPE,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def supply_chain_detached_jws_signing_input(
    *,
    method: str,
    path: str,
    created: str,
    nonce: str,
    request_digest: str,
    credential_id: str,
    workload_identity: str,
) -> bytes:
    """Return RFC 7797-style detached JWS signing input for the v2 profile."""

    protected = base64url_encode(supply_chain_jws_protected_header(credential_id))
    payload = supply_chain_asymmetric_envelope_payload(
        method=method,
        path=path,
        created=created,
        nonce=nonce,
        request_digest=request_digest,
        credential_id=credential_id,
        workload_identity=workload_identity,
    )
    return f"{protected}.{base64url_encode(payload)}".encode("ascii")
