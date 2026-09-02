"""Read-only operational status for configured supply-chain verifier keys."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Literal, TypedDict

from .config import Settings, SupplyChainVerifierPublicKey

VerifierKeyStatus = Literal[
    "ACTIVE",
    "EXPIRING_SOON",
    "NOT_YET_ACTIVE",
    "EXPIRED",
    "REVOKED",
]
VerifierKeyReadinessStatus = Literal["READY", "WARNING", "BLOCKED"]
VerifierKeyWarning = Literal[
    "NO_ED25519_KEYS",
    "NO_ACCEPTING_KEYS",
    "NO_OVERLAPPING_ACTIVE_KEYS",
    "ACTIVE_KEY_EXPIRING_SOON",
    "LEGACY_HMAC_ENABLED",
]


class VerifierKeyReadinessItem(TypedDict):
    credential_id: str
    verifier: str
    workload_identity: str
    key_fingerprint: str
    status: VerifierKeyStatus
    accepts_now: bool
    not_before: datetime | None
    not_after: datetime | None
    revoked_at: datetime | None


class SupplyChainKeyReadinessSnapshot(TypedDict):
    status: VerifierKeyReadinessStatus
    configuration_digest: str
    generated_at: datetime
    expiry_warning_days: int
    configured_key_count: int
    accepting_key_count: int
    future_key_count: int
    expiring_soon_key_count: int
    expired_key_count: int
    revoked_key_count: int
    overlap_ready: bool
    legacy_hmac_enabled: bool
    warnings: tuple[VerifierKeyWarning, ...]
    keys: tuple[VerifierKeyReadinessItem, ...]


def _canonical_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def supply_chain_keyring_configuration_digest(settings: Settings) -> str:
    """Return a stable, non-secret digest for cross-instance configuration checks."""

    canonical = {
        "ed25519_keys": [
            {
                "credential_id": key.credential_id,
                "key_fingerprint": key.key_fingerprint,
                "not_after": _canonical_timestamp(key.not_after),
                "not_before": _canonical_timestamp(key.not_before),
                "revoked_at": _canonical_timestamp(key.revoked_at),
                "verifier": key.verifier,
                "workload_identity": key.workload_identity,
            }
            for key in sorted(
                settings.supply_chain_verifier_public_keys,
                key=lambda item: (item.verifier, item.credential_id),
            )
        ],
        "expiry_warning_days": settings.supply_chain_key_expiry_warning_days,
        "hmac_credential_ids": sorted(
            credential.credential_id for credential in settings.supply_chain_verifier_credentials
        ),
        "legacy_hmac_enabled": settings.supply_chain_allow_legacy_hmac,
        "profile": "testops-supply-chain-keyring-v1",
    }
    serialized = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + sha256(serialized).hexdigest()


def _key_status(
    key: SupplyChainVerifierPublicKey,
    *,
    generated_at: datetime,
    warning_until: datetime,
) -> VerifierKeyStatus:
    if key.revoked_at is not None and generated_at >= key.revoked_at:
        return "REVOKED"
    if key.not_after is not None and generated_at >= key.not_after:
        return "EXPIRED"
    if key.not_before is not None and generated_at < key.not_before:
        return "NOT_YET_ACTIVE"
    if key.not_after is not None and key.not_after <= warning_until:
        return "EXPIRING_SOON"
    return "ACTIVE"


def collect_supply_chain_key_readiness(
    settings: Settings,
    *,
    generated_at: datetime | None = None,
) -> SupplyChainKeyReadinessSnapshot:
    """Return non-secret verifier key metadata and rotation readiness."""

    moment = (generated_at or datetime.now(UTC)).astimezone(UTC)
    warning_until = moment + timedelta(days=settings.supply_chain_key_expiry_warning_days)
    keys = tuple(
        sorted(
            settings.supply_chain_verifier_public_keys,
            key=lambda item: (item.verifier, item.credential_id),
        )
    )
    items: list[VerifierKeyReadinessItem] = []
    statuses: list[VerifierKeyStatus] = []
    for key in keys:
        key_status = _key_status(key, generated_at=moment, warning_until=warning_until)
        statuses.append(key_status)
        items.append(
            {
                "credential_id": key.credential_id,
                "verifier": key.verifier,
                "workload_identity": key.workload_identity,
                "key_fingerprint": key.key_fingerprint,
                "status": key_status,
                "accepts_now": key_status in {"ACTIVE", "EXPIRING_SOON"},
                "not_before": key.not_before,
                "not_after": key.not_after,
                "revoked_at": key.revoked_at,
            }
        )

    accepting_key_count = sum(status in {"ACTIVE", "EXPIRING_SOON"} for status in statuses)
    future_key_count = statuses.count("NOT_YET_ACTIVE")
    expiring_soon_key_count = statuses.count("EXPIRING_SOON")
    expired_key_count = statuses.count("EXPIRED")
    revoked_key_count = statuses.count("REVOKED")
    rotation_groups = {(key.verifier, key.workload_identity) for key in keys}
    accepting_by_group = Counter(
        (key.verifier, key.workload_identity)
        for key, status in zip(keys, statuses, strict=True)
        if status in {"ACTIVE", "EXPIRING_SOON"}
    )
    overlap_ready = bool(rotation_groups) and all(
        accepting_by_group[group] >= 2 for group in rotation_groups
    )
    warnings: list[VerifierKeyWarning] = []
    if not keys:
        warnings.append("NO_ED25519_KEYS")
    elif accepting_key_count == 0:
        warnings.append("NO_ACCEPTING_KEYS")
    elif not overlap_ready:
        warnings.append("NO_OVERLAPPING_ACTIVE_KEYS")
    if expiring_soon_key_count:
        warnings.append("ACTIVE_KEY_EXPIRING_SOON")
    if settings.supply_chain_allow_legacy_hmac:
        warnings.append("LEGACY_HMAC_ENABLED")

    if accepting_key_count == 0:
        readiness: VerifierKeyReadinessStatus = "BLOCKED"
    elif warnings:
        readiness = "WARNING"
    else:
        readiness = "READY"
    return {
        "status": readiness,
        "configuration_digest": supply_chain_keyring_configuration_digest(settings),
        "generated_at": moment,
        "expiry_warning_days": settings.supply_chain_key_expiry_warning_days,
        "configured_key_count": len(keys),
        "accepting_key_count": accepting_key_count,
        "future_key_count": future_key_count,
        "expiring_soon_key_count": expiring_soon_key_count,
        "expired_key_count": expired_key_count,
        "revoked_key_count": revoked_key_count,
        "overlap_ready": overlap_ready,
        "legacy_hmac_enabled": settings.supply_chain_allow_legacy_hmac,
        "warnings": tuple(warnings),
        "keys": tuple(items),
    }
