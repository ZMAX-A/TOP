"""Dedicated signed-envelope authentication for supply-chain verifier services."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid5

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Depends, Header, Request

from testops.contracts.supply_chain_envelope import (
    ASYMMETRIC_ENVELOPE_PROFILE,
    JWS_TYPE,
    LEGACY_ENVELOPE_PROFILE,
    base64url_encode,
    supply_chain_detached_jws_signing_input,
    supply_chain_envelope_signature_base,
    supply_chain_jws_protected_header,
)

from .config import SupplyChainVerifierCredential, SupplyChainVerifierPublicKey
from .persistence import utc_now
from .services import ServiceError

ENVELOPE_PROFILE = LEGACY_ENVELOPE_PROFILE
ENVELOPE_SIGNATURE_PATTERN = re.compile(r"^hmac-sha256=(?P<digest>[0-9a-f]{64})$")
VERIFIER_ACTOR_NAMESPACE = UUID("c47f4642-d580-54f9-8f6d-3760f32c4c20")
DUMMY_HMAC_SECRET = b"testops-invalid-verifier-credential" * 2
DUMMY_ED25519_PUBLIC_KEY = b"\x00" * 32


class SupplyChainVerifierAuthenticationRequired(ServiceError):
    status_code = 401


class SupplyChainVerifierUnavailable(ServiceError):
    status_code = 503


@dataclass(frozen=True, slots=True)
class SupplyChainVerifierPrincipal:
    verifier: str
    credential_id: str
    nonce: str
    issued_at: datetime
    request_digest: str
    signature_digest: str
    envelope_profile: str
    signature_algorithm: str
    workload_identity: str | None
    key_fingerprint: str | None

    @property
    def actor_id(self) -> UUID:
        return uuid5(
            VERIFIER_ACTOR_NAMESPACE,
            self.workload_identity or self.credential_id,
        )


def _base64url_decode(value: str) -> bytes:
    if not value or "=" in value or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError from exc
    if base64url_encode(decoded) != value:
        raise ValueError
    return decoded


def _authentication_failure() -> SupplyChainVerifierAuthenticationRequired:
    return SupplyChainVerifierAuthenticationRequired("invalid supply-chain verifier envelope")


def _verify_asymmetric_envelope(
    *,
    signature: str,
    credential_id: str,
    public_keys: tuple[SupplyChainVerifierPublicKey, ...],
    method: str,
    path: str,
    created: str,
    nonce: str,
    request_digest: str,
) -> SupplyChainVerifierPrincipal:
    try:
        protected_segment, detached_payload, signature_segment = signature.split(".")
        if detached_payload:
            raise ValueError
        protected_bytes = _base64url_decode(protected_segment)
        protected = json.loads(protected_bytes)
        expected_header = {
            "alg": "EdDSA",
            "kid": credential_id,
            "typ": JWS_TYPE,
        }
        if protected != expected_header or protected_bytes != supply_chain_jws_protected_header(
            credential_id
        ):
            raise ValueError
        signature_bytes = _base64url_decode(signature_segment)
        if len(signature_bytes) != 64:
            raise ValueError
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise _authentication_failure() from exc

    public_key = next(
        (item for item in public_keys if item.credential_id == credential_id),
        None,
    )
    workload_identity = (
        public_key.workload_identity
        if public_key is not None
        else "spiffe://invalid.testops/verifier"
    )
    signing_input = supply_chain_detached_jws_signing_input(
        method=method,
        path=path,
        created=created,
        nonce=nonce,
        request_digest=request_digest,
        credential_id=credential_id,
        workload_identity=workload_identity,
    )
    key_bytes = public_key.public_key if public_key is not None else DUMMY_ED25519_PUBLIC_KEY
    try:
        Ed25519PublicKey.from_public_bytes(key_bytes).verify(signature_bytes, signing_input)
    except (InvalidSignature, ValueError) as exc:
        raise _authentication_failure() from exc
    if public_key is None:
        raise _authentication_failure()
    return SupplyChainVerifierPrincipal(
        verifier=public_key.verifier,
        credential_id=public_key.credential_id,
        nonce=nonce,
        issued_at=datetime.fromtimestamp(int(created), UTC),
        request_digest=request_digest,
        signature_digest="sha256:" + hashlib.sha256(signature_bytes).hexdigest(),
        envelope_profile=ASYMMETRIC_ENVELOPE_PROFILE,
        signature_algorithm="ED25519",
        workload_identity=public_key.workload_identity,
        key_fingerprint=public_key.key_fingerprint,
    )


def _verify_legacy_hmac_envelope(
    *,
    signature: str,
    credential_id: str,
    credentials: tuple[SupplyChainVerifierCredential, ...],
    method: str,
    path: str,
    created: str,
    nonce: str,
    request_digest: str,
) -> SupplyChainVerifierPrincipal:
    match = ENVELOPE_SIGNATURE_PATTERN.fullmatch(signature)
    if match is None:
        raise _authentication_failure()
    credential = next(
        (item for item in credentials if item.credential_id == credential_id),
        None,
    )
    secret = credential.secret.encode("utf-8") if credential is not None else DUMMY_HMAC_SECRET
    signature_base = supply_chain_envelope_signature_base(
        method=method,
        path=path,
        created=created,
        nonce=nonce,
        request_digest=request_digest,
    )
    expected = hmac.new(secret, signature_base, hashlib.sha256).hexdigest()
    if credential is None or not hmac.compare_digest(match.group("digest"), expected):
        raise _authentication_failure()
    signature_bytes = bytes.fromhex(match.group("digest"))
    return SupplyChainVerifierPrincipal(
        verifier=credential.verifier,
        credential_id=credential.credential_id,
        nonce=nonce,
        issued_at=datetime.fromtimestamp(int(created), UTC),
        request_digest=request_digest,
        signature_digest="sha256:" + hashlib.sha256(signature_bytes).hexdigest(),
        envelope_profile=LEGACY_ENVELOPE_PROFILE,
        signature_algorithm="HMAC-SHA256",
        workload_identity=None,
        key_fingerprint=None,
    )


async def current_supply_chain_verifier(
    request: Request,
    credential_id: Annotated[
        str | None,
        Header(alias="X-TestOps-Verifier-Key-Id"),
    ] = None,
    created: Annotated[
        str | None,
        Header(alias="X-TestOps-Envelope-Created"),
    ] = None,
    nonce: Annotated[
        str | None,
        Header(alias="X-TestOps-Envelope-Nonce"),
    ] = None,
    signature: Annotated[
        str | None,
        Header(alias="X-TestOps-Envelope-Signature"),
    ] = None,
) -> SupplyChainVerifierPrincipal:
    settings = request.app.state.settings
    public_keys: tuple[SupplyChainVerifierPublicKey, ...] = (
        settings.supply_chain_verifier_public_keys
    )
    credentials: tuple[SupplyChainVerifierCredential, ...] = (
        settings.supply_chain_verifier_credentials
        if settings.supply_chain_allow_legacy_hmac
        else ()
    )
    if not public_keys and not credentials:
        raise SupplyChainVerifierUnavailable(
            "supply-chain verifier service credentials are not configured"
        )
    if None in {credential_id, created, nonce, signature}:
        raise _authentication_failure()
    assert credential_id is not None
    assert created is not None
    assert nonce is not None
    assert signature is not None

    try:
        issued_epoch = int(created)
        if str(issued_epoch) != created:
            raise ValueError
        parsed_nonce = UUID(nonce)
        if str(parsed_nonce) != nonce:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise _authentication_failure() from exc
    now = utc_now()
    now_epoch = int(now.timestamp())
    if issued_epoch < now_epoch - settings.supply_chain_envelope_ttl_seconds:
        raise _authentication_failure()
    if issued_epoch > now_epoch + settings.supply_chain_envelope_future_skew_seconds:
        raise _authentication_failure()

    body = await request.body()
    request_digest = "sha256:" + hashlib.sha256(body).hexdigest()
    verification_arguments = {
        "signature": signature,
        "credential_id": credential_id,
        "method": request.method,
        "path": request.url.path,
        "created": created,
        "nonce": nonce,
        "request_digest": request_digest,
    }
    if signature.startswith("hmac-sha256="):
        if not credentials:
            raise _authentication_failure()
        return _verify_legacy_hmac_envelope(
            credentials=credentials,
            **verification_arguments,
        )
    return _verify_asymmetric_envelope(
        public_keys=public_keys,
        **verification_arguments,
    )


SupplyChainVerifierAuth = Annotated[
    SupplyChainVerifierPrincipal,
    Depends(current_supply_chain_verifier),
]
