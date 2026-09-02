"""Environment-backed settings for the TestOps control plane."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import urlsplit

DEFAULT_DATABASE_URL = "postgresql+asyncpg://testops:change-me-local-only@localhost:5432/testops"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"


def _environment_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _environment_tuple(name: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, "").split(",") if value.strip())


VERIFIER_CREDENTIAL_ID_PATTERN = re.compile(
    r"^(?P<verifier>[a-z0-9][a-z0-9._-]{2,63})/"
    r"(?P<key_id>[A-Za-z0-9][A-Za-z0-9._-]{0,63})$"
)
VERIFIER_KEY_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@dataclass(frozen=True, slots=True)
class SupplyChainVerifierCredential:
    credential_id: str
    verifier: str
    secret: str = field(repr=False)

    def __post_init__(self) -> None:
        match = VERIFIER_CREDENTIAL_ID_PATTERN.fullmatch(self.credential_id)
        if match is None or match.group("verifier") != self.verifier:
            raise ValueError("supply-chain verifier credential_id must be '<verifier>/<key-id>'")
        if len(self.secret.encode("utf-8")) < 32:
            raise ValueError("supply-chain verifier HMAC secrets must be at least 32 bytes")


@dataclass(frozen=True, slots=True)
class SupplyChainVerifierPublicKey:
    credential_id: str
    verifier: str
    workload_identity: str
    public_key: bytes = field(repr=False)
    not_before: datetime | None = None
    not_after: datetime | None = None
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        match = VERIFIER_CREDENTIAL_ID_PATTERN.fullmatch(self.credential_id)
        if match is None or match.group("verifier") != self.verifier:
            raise ValueError("supply-chain verifier credential_id must be '<verifier>/<key-id>'")
        parsed_identity = urlsplit(self.workload_identity)
        if parsed_identity.scheme not in {"https", "spiffe"} or not parsed_identity.netloc:
            raise ValueError(
                "supply-chain verifier workload_identity must be an HTTPS or SPIFFE URI"
            )
        if len(self.workload_identity) > 512:
            raise ValueError("supply-chain verifier workload_identity cannot exceed 512 characters")
        if len(self.public_key) != 32:
            raise ValueError("supply-chain verifier Ed25519 public keys must be exactly 32 bytes")
        for field_name in ("not_before", "not_after", "revoked_at"):
            value = getattr(self, field_name)
            if value is None:
                continue
            if (
                value.tzinfo is None
                or value.utcoffset() is None
                or value.utcoffset() != timedelta(0)
            ):
                raise ValueError(
                    f"supply-chain verifier {field_name} must be a timezone-aware UTC timestamp"
                )
            normalized = value.astimezone(UTC)
            if normalized.microsecond:
                raise ValueError(
                    f"supply-chain verifier {field_name} must have whole-second precision"
                )
            object.__setattr__(self, field_name, normalized)
        if (
            self.not_before is not None
            and self.not_after is not None
            and self.not_before >= self.not_after
        ):
            raise ValueError("supply-chain verifier not_before must be earlier than not_after")

    @property
    def key_fingerprint(self) -> str:
        return "sha256:" + sha256(self.public_key).hexdigest()

    def accepts(self, *, issued_at: datetime, verified_at: datetime) -> bool:
        """Return whether this key may authenticate the envelope at both relevant times."""

        if self.not_before is not None and (
            issued_at < self.not_before or verified_at < self.not_before
        ):
            return False
        if self.not_after is not None and (
            issued_at >= self.not_after or verified_at >= self.not_after
        ):
            return False
        if self.revoked_at is not None and (
            issued_at >= self.revoked_at or verified_at >= self.revoked_at
        ):
            return False
        return True


def _base64url_decode(value: str, *, name: str) -> bytes:
    if not value or "=" in value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError(f"{name} must use unpadded base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{name} must use valid unpadded base64url") from exc
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value:
        raise ValueError(f"{name} must use canonical unpadded base64url")
    return decoded


def _parse_verifier_key_timestamp(value: str, *, name: str) -> datetime:
    if VERIFIER_KEY_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must use canonical UTC format YYYY-MM-DDTHH:MM:SSZ")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid UTC timestamp") from exc


def _environment_verifier_public_keys(
    name: str,
) -> tuple[SupplyChainVerifierPublicKey, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return ()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be a JSON array") from exc
    if not isinstance(document, list):
        raise ValueError(f"{name} must be a JSON array")
    if len(document) > 16:
        raise ValueError(f"{name} cannot contain more than 16 public keys")
    keys: list[SupplyChainVerifierPublicKey] = []
    seen: set[str] = set()
    required_fields = {"credential_id", "workload_identity", "public_key"}
    optional_fields = {"not_before", "not_after", "revoked_at"}
    for item in document:
        if (
            not isinstance(item, dict)
            or not required_fields.issubset(item)
            or not set(item).issubset(required_fields | optional_fields)
        ):
            raise ValueError(
                f"{name} entries require credential_id, workload_identity and public_key; "
                "only not_before, not_after and revoked_at are optional"
            )
        if not all(isinstance(item[field], str) for field in item):
            raise ValueError(f"{name} entry values must be strings")
        credential_id = item["credential_id"]
        if credential_id in seen:
            raise ValueError(f"{name} contains duplicate credential id: {credential_id}")
        match = VERIFIER_CREDENTIAL_ID_PATTERN.fullmatch(credential_id)
        if match is None:
            raise ValueError(
                f"{name} credential ids must use '<verifier>/<key-id>' with safe characters"
            )
        keys.append(
            SupplyChainVerifierPublicKey(
                credential_id=credential_id,
                verifier=match.group("verifier"),
                workload_identity=item["workload_identity"],
                public_key=_base64url_decode(
                    item["public_key"],
                    name=f"{name} public_key",
                ),
                not_before=(
                    _parse_verifier_key_timestamp(
                        item["not_before"],
                        name=f"{name} not_before",
                    )
                    if "not_before" in item
                    else None
                ),
                not_after=(
                    _parse_verifier_key_timestamp(
                        item["not_after"],
                        name=f"{name} not_after",
                    )
                    if "not_after" in item
                    else None
                ),
                revoked_at=(
                    _parse_verifier_key_timestamp(
                        item["revoked_at"],
                        name=f"{name} revoked_at",
                    )
                    if "revoked_at" in item
                    else None
                ),
            )
        )
        seen.add(credential_id)
    return tuple(keys)


def _environment_verifier_credentials(
    name: str,
) -> tuple[SupplyChainVerifierCredential, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return ()
    credentials: list[SupplyChainVerifierCredential] = []
    seen: set[str] = set()
    for value in raw.split(","):
        credential_id, separator, secret = value.strip().partition("=")
        if not separator or not credential_id or not secret:
            raise ValueError(
                f"{name} must contain comma-separated '<verifier>/<key-id>=<secret>' entries"
            )
        if credential_id in seen:
            raise ValueError(f"{name} contains duplicate credential id: {credential_id}")
        match = VERIFIER_CREDENTIAL_ID_PATTERN.fullmatch(credential_id)
        if match is None:
            raise ValueError(
                f"{name} credential ids must use '<verifier>/<key-id>' with safe characters"
            )
        credentials.append(
            SupplyChainVerifierCredential(
                credential_id=credential_id,
                verifier=match.group("verifier"),
                secret=secret,
            )
        )
        seen.add(credential_id)
    if len(credentials) > 16:
        raise ValueError(f"{name} cannot contain more than 16 credentials")
    return tuple(credentials)


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = DEFAULT_DATABASE_URL
    redis_url: str = DEFAULT_REDIS_URL
    database_echo: bool = False
    auto_create_schema: bool = False
    runner_callback_token: str | None = field(default=None, repr=False)
    bootstrap_admin_token: str | None = field(default=None, repr=False)
    metrics_token: str | None = field(default=None, repr=False)
    session_ttl_hours: int = 8
    cors_origins: tuple[str, ...] = ("http://127.0.0.1:5173", "http://localhost:5173")
    minio_endpoint: str | None = None
    minio_access_key: str | None = None
    minio_secret_key: str | None = field(default=None, repr=False)
    minio_bucket: str = "testops-artifacts"
    minio_region: str = "us-east-1"
    artifact_url_ttl_seconds: int = 300
    run_event_poll_seconds: float = 0.5
    run_event_heartbeat_seconds: float = 15.0
    live_view_ticket_ttl_seconds: int = 30
    runner_heartbeat_ttl_seconds: int = 45
    run_dispatch_start_timeout_seconds: int = 300
    github_release_token: str | None = field(default=None, repr=False)
    github_release_timeout_seconds: float = 15.0
    github_release_max_manifest_bytes: int = 5_000_000
    supply_chain_policy_version: str = "testops-supply-chain-v1"
    supply_chain_allowed_verifiers: tuple[str, ...] = ()
    supply_chain_allowed_certificate_issuers: tuple[str, ...] = ()
    supply_chain_allowed_certificate_identities: tuple[str, ...] = ()
    supply_chain_allowed_builder_ids: tuple[str, ...] = ()
    supply_chain_allowed_source_repositories: tuple[str, ...] = ()
    supply_chain_verifier_public_keys: tuple[SupplyChainVerifierPublicKey, ...] = field(
        default=(),
        repr=False,
    )
    supply_chain_verifier_credentials: tuple[SupplyChainVerifierCredential, ...] = field(
        default=(),
        repr=False,
    )
    supply_chain_allow_legacy_hmac: bool = False
    supply_chain_envelope_ttl_seconds: int = 300
    supply_chain_envelope_future_skew_seconds: int = 30
    supply_chain_key_expiry_warning_days: int = 30

    def __post_init__(self) -> None:
        if not self.redis_url.strip():
            raise ValueError("REDIS_URL cannot be empty")
        if not 10 <= self.live_view_ticket_ttl_seconds <= 120:
            raise ValueError("LIVE_VIEW_TICKET_TTL_SECONDS must be between 10 and 120")
        if not 1 <= self.supply_chain_key_expiry_warning_days <= 365:
            raise ValueError("supply-chain key expiry warning days must be between 1 and 365")
        if not 1 <= self.github_release_timeout_seconds <= 60:
            raise ValueError("GITHUB_RELEASE_TIMEOUT_SECONDS must be between 1 and 60")
        if not 1_024 <= self.github_release_max_manifest_bytes <= 10_000_000:
            raise ValueError(
                "GITHUB_RELEASE_MAX_MANIFEST_BYTES must be between 1024 and 10000000"
            )
        credential_ids = [
            item.credential_id
            for item in (
                *self.supply_chain_verifier_public_keys,
                *self.supply_chain_verifier_credentials,
            )
        ]
        if len(set(credential_ids)) != len(credential_ids):
            raise ValueError("supply-chain verifier credential ids must be unique across keyrings")
        if len(credential_ids) > 16:
            raise ValueError(
                "supply-chain verifier keyrings cannot contain more than 16 credentials"
            )
        key_fingerprints = [item.key_fingerprint for item in self.supply_chain_verifier_public_keys]
        if len(set(key_fingerprints)) != len(key_fingerprints):
            raise ValueError("supply-chain verifier Ed25519 public keys must be unique")

    @classmethod
    def from_environment(cls) -> Settings:
        session_ttl_hours = int(os.getenv("SESSION_TTL_HOURS", "8"))
        if not 1 <= session_ttl_hours <= 24 * 30:
            raise ValueError("SESSION_TTL_HOURS must be between 1 and 720")
        artifact_url_ttl_seconds = int(os.getenv("ARTIFACT_URL_TTL_SECONDS", "300"))
        if not 30 <= artifact_url_ttl_seconds <= 3600:
            raise ValueError("ARTIFACT_URL_TTL_SECONDS must be between 30 and 3600")
        run_event_poll_seconds = float(os.getenv("RUN_EVENT_POLL_SECONDS", "0.5"))
        if not 0.1 <= run_event_poll_seconds <= 10:
            raise ValueError("RUN_EVENT_POLL_SECONDS must be between 0.1 and 10")
        run_event_heartbeat_seconds = float(os.getenv("RUN_EVENT_HEARTBEAT_SECONDS", "15"))
        if not 5 <= run_event_heartbeat_seconds <= 60:
            raise ValueError("RUN_EVENT_HEARTBEAT_SECONDS must be between 5 and 60")
        live_view_ticket_ttl_seconds = int(os.getenv("LIVE_VIEW_TICKET_TTL_SECONDS", "30"))
        if not 10 <= live_view_ticket_ttl_seconds <= 120:
            raise ValueError("LIVE_VIEW_TICKET_TTL_SECONDS must be between 10 and 120")
        runner_heartbeat_ttl_seconds = int(os.getenv("RUNNER_HEARTBEAT_TTL_SECONDS", "45"))
        if not 15 <= runner_heartbeat_ttl_seconds <= 600:
            raise ValueError("RUNNER_HEARTBEAT_TTL_SECONDS must be between 15 and 600")
        run_dispatch_start_timeout_seconds = int(
            os.getenv("RUN_DISPATCH_START_TIMEOUT_SECONDS", "300")
        )
        if not 30 <= run_dispatch_start_timeout_seconds <= 3600:
            raise ValueError("RUN_DISPATCH_START_TIMEOUT_SECONDS must be between 30 and 3600")
        github_release_timeout_seconds = float(
            os.getenv("GITHUB_RELEASE_TIMEOUT_SECONDS", "15")
        )
        github_release_max_manifest_bytes = int(
            os.getenv("GITHUB_RELEASE_MAX_MANIFEST_BYTES", "5000000")
        )
        cors_origins = tuple(
            origin.strip()
            for origin in os.getenv(
                "CORS_ORIGINS",
                "http://127.0.0.1:5173,http://localhost:5173",
            ).split(",")
            if origin.strip()
        )
        supply_chain_policy_version = os.getenv(
            "SUPPLY_CHAIN_POLICY_VERSION",
            "testops-supply-chain-v1",
        ).strip()
        if not supply_chain_policy_version:
            raise ValueError("SUPPLY_CHAIN_POLICY_VERSION cannot be empty")
        supply_chain_envelope_ttl_seconds = int(
            os.getenv("SUPPLY_CHAIN_ENVELOPE_TTL_SECONDS", "300")
        )
        if not 30 <= supply_chain_envelope_ttl_seconds <= 900:
            raise ValueError("SUPPLY_CHAIN_ENVELOPE_TTL_SECONDS must be between 30 and 900")
        supply_chain_envelope_future_skew_seconds = int(
            os.getenv("SUPPLY_CHAIN_ENVELOPE_FUTURE_SKEW_SECONDS", "30")
        )
        if not 0 <= supply_chain_envelope_future_skew_seconds <= 300:
            raise ValueError("SUPPLY_CHAIN_ENVELOPE_FUTURE_SKEW_SECONDS must be between 0 and 300")
        if supply_chain_envelope_future_skew_seconds >= supply_chain_envelope_ttl_seconds:
            raise ValueError(
                "SUPPLY_CHAIN_ENVELOPE_FUTURE_SKEW_SECONDS must be less than the envelope TTL"
            )
        supply_chain_verifier_public_keys = _environment_verifier_public_keys(
            "SUPPLY_CHAIN_VERIFIER_ED25519_KEYS"
        )
        supply_chain_verifier_credentials = _environment_verifier_credentials(
            "SUPPLY_CHAIN_VERIFIER_HMAC_KEYS"
        )
        supply_chain_key_expiry_warning_days = int(
            os.getenv("SUPPLY_CHAIN_KEY_EXPIRY_WARNING_DAYS", "30")
        )
        return cls(
            database_url=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL),
            redis_url=os.getenv("REDIS_URL", DEFAULT_REDIS_URL),
            database_echo=_environment_bool("DATABASE_ECHO", False),
            auto_create_schema=_environment_bool("AUTO_CREATE_SCHEMA", False),
            runner_callback_token=os.getenv("RUNNER_CALLBACK_TOKEN") or None,
            bootstrap_admin_token=os.getenv("BOOTSTRAP_ADMIN_TOKEN") or None,
            metrics_token=os.getenv("METRICS_TOKEN") or None,
            session_ttl_hours=session_ttl_hours,
            cors_origins=cors_origins,
            minio_endpoint=os.getenv("MINIO_PUBLIC_ENDPOINT")
            or os.getenv("MINIO_ENDPOINT")
            or None,
            minio_access_key=os.getenv("MINIO_ACCESS_KEY") or os.getenv("MINIO_ROOT_USER") or None,
            minio_secret_key=os.getenv("MINIO_SECRET_KEY")
            or os.getenv("MINIO_ROOT_PASSWORD")
            or None,
            minio_bucket=os.getenv("MINIO_BUCKET", "testops-artifacts"),
            minio_region=os.getenv("MINIO_REGION", "us-east-1"),
            artifact_url_ttl_seconds=artifact_url_ttl_seconds,
            run_event_poll_seconds=run_event_poll_seconds,
            run_event_heartbeat_seconds=run_event_heartbeat_seconds,
            live_view_ticket_ttl_seconds=live_view_ticket_ttl_seconds,
            runner_heartbeat_ttl_seconds=runner_heartbeat_ttl_seconds,
            run_dispatch_start_timeout_seconds=run_dispatch_start_timeout_seconds,
            github_release_token=os.getenv("GITHUB_RELEASE_TOKEN") or None,
            github_release_timeout_seconds=github_release_timeout_seconds,
            github_release_max_manifest_bytes=github_release_max_manifest_bytes,
            supply_chain_policy_version=supply_chain_policy_version,
            supply_chain_allowed_verifiers=_environment_tuple("SUPPLY_CHAIN_ALLOWED_VERIFIERS"),
            supply_chain_allowed_certificate_issuers=_environment_tuple(
                "SUPPLY_CHAIN_ALLOWED_CERTIFICATE_ISSUERS"
            ),
            supply_chain_allowed_certificate_identities=_environment_tuple(
                "SUPPLY_CHAIN_ALLOWED_CERTIFICATE_IDENTITIES"
            ),
            supply_chain_allowed_builder_ids=_environment_tuple("SUPPLY_CHAIN_ALLOWED_BUILDER_IDS"),
            supply_chain_allowed_source_repositories=_environment_tuple(
                "SUPPLY_CHAIN_ALLOWED_SOURCE_REPOSITORIES"
            ),
            supply_chain_verifier_public_keys=supply_chain_verifier_public_keys,
            supply_chain_verifier_credentials=supply_chain_verifier_credentials,
            supply_chain_allow_legacy_hmac=_environment_bool(
                "SUPPLY_CHAIN_ALLOW_LEGACY_HMAC",
                False,
            ),
            supply_chain_envelope_ttl_seconds=supply_chain_envelope_ttl_seconds,
            supply_chain_envelope_future_skew_seconds=(supply_chain_envelope_future_skew_seconds),
            supply_chain_key_expiry_warning_days=supply_chain_key_expiry_warning_days,
        )
