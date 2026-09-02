"""Fail-closed multi-instance verifier keyring rollout gate."""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import stat
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

MAX_INSTANCES = 32
MAX_TOKEN_BYTES = 4096
MAX_RESPONSE_BYTES = 256 * 1024
READINESS_PATH = "/api/v1/admin/supply-chain/verifier-keys"
INSTANCE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
READINESS_STATUSES = frozenset({"READY", "WARNING", "BLOCKED"})
READINESS_WARNINGS = frozenset(
    {
        "NO_ED25519_KEYS",
        "NO_ACCEPTING_KEYS",
        "NO_OVERLAPPING_ACTIVE_KEYS",
        "ACTIVE_KEY_EXPIRING_SOON",
        "LEGACY_HMAC_ENABLED",
    }
)

FleetStatus = Literal["READY", "NOT_READY", "DRIFT"]


class FleetPreflightError(RuntimeError):
    """Safe operator-facing fleet validation or transport failure."""

    def __init__(self, message: str, *, instance: str | None = None) -> None:
        super().__init__(message)
        self.instance = instance


@dataclass(frozen=True, slots=True)
class FleetInstance:
    name: str
    origin: str


@dataclass(frozen=True, slots=True)
class InstanceReadiness:
    name: str
    status: str
    configuration_digest: str
    generated_at: str
    configured_key_count: int
    accepting_key_count: int
    overlap_ready: bool
    legacy_hmac_enabled: bool
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FleetReport:
    status: FleetStatus
    configuration_digest: str | None
    expected_digest: str | None
    instances: tuple[InstanceReadiness, ...]


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def _read_bounded_file(path: Path, *, limit: int, label: str) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise FleetPreflightError(f"cannot inspect {label} file") from exc
    if size <= 0 or size > limit:
        raise FleetPreflightError(f"{label} file size must be between 1 and {limit} bytes")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise FleetPreflightError(f"cannot read {label} file") from exc
    if len(content) != size:
        raise FleetPreflightError(f"{label} file changed while being read")
    return content


def load_bearer_token(path: Path) -> str:
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise FleetPreflightError("cannot inspect token file") from exc
    if not stat.S_ISREG(mode):
        raise FleetPreflightError("token file must be a regular file")
    if os.name != "nt" and stat.S_IMODE(mode) & 0o077:
        raise FleetPreflightError("token file must not be readable by group or others")
    content = _read_bounded_file(path, limit=MAX_TOKEN_BYTES, label="token")
    try:
        token = content.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise FleetPreflightError("token file must contain UTF-8 text") from exc
    if not token or any(character.isspace() for character in token):
        raise FleetPreflightError("token file must contain one non-whitespace bearer token")
    return token


def _validate_origin(origin: str, *, allow_http: bool) -> str:
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.hostname is None:
        raise FleetPreflightError("instance origin must be absolute HTTP or HTTPS")
    try:
        port = parsed.port
    except ValueError as exc:
        raise FleetPreflightError("instance origin contains an invalid port") from exc
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise FleetPreflightError(
            "instance origin must not contain credentials, query, or fragment"
        )
    if parsed.path not in {"", "/"}:
        raise FleetPreflightError("instance origin must not contain a path prefix")
    if parsed.scheme != "https":
        if not allow_http:
            raise FleetPreflightError("HTTP requires the explicit --allow-http development switch")
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise FleetPreflightError("the --allow-http switch is restricted to loopback hosts")
    hostname = parsed.hostname.casefold().rstrip(".")
    if not hostname:
        raise FleetPreflightError("instance origin must contain a hostname")
    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if parsed.scheme == "https" else 80
    port_suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme}://{host}{port_suffix}"


def parse_instances(values: Sequence[str], *, allow_http: bool) -> tuple[FleetInstance, ...]:
    if len(values) < 2:
        raise FleetPreflightError("at least two direct API instances are required")
    if len(values) > MAX_INSTANCES:
        raise FleetPreflightError(f"at most {MAX_INSTANCES} API instances are allowed")

    instances: list[FleetInstance] = []
    names: set[str] = set()
    origins: set[str] = set()
    for value in values:
        name, separator, origin = value.partition("=")
        if not separator or INSTANCE_NAME_PATTERN.fullmatch(name) is None:
            raise FleetPreflightError(
                "each instance must use NAME=ORIGIN with a stable alphanumeric name"
            )
        normalized_origin = _validate_origin(origin, allow_http=allow_http)
        if name in names:
            raise FleetPreflightError("instance names must be unique")
        if normalized_origin in origins:
            raise FleetPreflightError("instance origins must be unique")
        names.add(name)
        origins.add(normalized_origin)
        instances.append(FleetInstance(name=name, origin=normalized_origin))
    return tuple(instances)


def _bounded_response(response: Any) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise FleetPreflightError("instance response has an invalid content length") from exc
        if declared_length < 0 or declared_length > MAX_RESPONSE_BYTES:
            raise FleetPreflightError("instance response exceeds the size limit")
    content = response.read(MAX_RESPONSE_BYTES + 1)
    if len(content) > MAX_RESPONSE_BYTES:
        raise FleetPreflightError("instance response exceeds the size limit")
    return content


def _required_int(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FleetPreflightError("instance response does not match the readiness contract")
    return value


def _required_bool(payload: dict[str, Any], field: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise FleetPreflightError("instance response does not match the readiness contract")
    return value


def parse_readiness_response(instance: FleetInstance, content: bytes) -> InstanceReadiness:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FleetPreflightError(
            "instance response is not valid UTF-8 JSON", instance=instance.name
        ) from exc
    if not isinstance(payload, dict):
        raise FleetPreflightError(
            "instance response does not match the readiness contract", instance=instance.name
        )
    status = payload.get("status")
    digest = payload.get("configuration_digest")
    generated_at = payload.get("generated_at")
    warnings = payload.get("warnings")
    if (
        status not in READINESS_STATUSES
        or not isinstance(digest, str)
        or DIGEST_PATTERN.fullmatch(digest) is None
        or not isinstance(generated_at, str)
        or not generated_at
        or not isinstance(warnings, list)
        or any(warning not in READINESS_WARNINGS for warning in warnings)
    ):
        raise FleetPreflightError(
            "instance response does not match the readiness contract", instance=instance.name
        )
    try:
        parsed_generated_at = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if parsed_generated_at.utcoffset() != timedelta(0):
            raise ValueError
        configured_key_count = _required_int(payload, "configured_key_count")
        accepting_key_count = _required_int(payload, "accepting_key_count")
        overlap_ready = _required_bool(payload, "overlap_ready")
        legacy_hmac_enabled = _required_bool(payload, "legacy_hmac_enabled")
    except FleetPreflightError as exc:
        raise FleetPreflightError(str(exc), instance=instance.name) from exc
    except ValueError as exc:
        raise FleetPreflightError(
            "instance response does not match the readiness contract", instance=instance.name
        ) from exc
    return InstanceReadiness(
        name=instance.name,
        status=status,
        configuration_digest=digest,
        generated_at=generated_at,
        configured_key_count=configured_key_count,
        accepting_key_count=accepting_key_count,
        overlap_ready=overlap_ready,
        legacy_hmac_enabled=legacy_hmac_enabled,
        warnings=tuple(warnings),
    )


def fetch_instance_readiness(
    instance: FleetInstance,
    *,
    token: str,
    timeout_seconds: float,
    ca_file: Path | None = None,
) -> InstanceReadiness:
    try:
        context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
        opener = build_opener(_RejectRedirects(), HTTPSHandler(context=context))
    except (OSError, ssl.SSLError) as exc:
        raise FleetPreflightError("cannot load the approved TLS trust store") from exc
    request = Request(
        instance.origin + READINESS_PATH,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Cache-Control": "no-cache",
            "User-Agent": "testops-keyring-fleet-preflight/1",
        },
        method="GET",
    )
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise FleetPreflightError(
                    "instance returned a non-success status", instance=instance.name
                )
            content = _bounded_response(response)
    except FleetPreflightError as exc:
        if exc.instance is not None:
            raise
        raise FleetPreflightError(str(exc), instance=instance.name) from exc
    except HTTPError as exc:
        raise FleetPreflightError(
            "instance rejected the readiness request", instance=instance.name
        ) from exc
    except (OSError, TimeoutError, URLError) as exc:
        raise FleetPreflightError(
            "instance readiness request failed", instance=instance.name
        ) from exc
    return parse_readiness_response(instance, content)


def evaluate_fleet(
    snapshots: Sequence[InstanceReadiness],
    *,
    expected_digest: str | None = None,
    allow_warning: bool = False,
) -> FleetReport:
    if len(snapshots) < 2:
        raise FleetPreflightError("at least two readiness snapshots are required")
    if expected_digest is not None and DIGEST_PATTERN.fullmatch(expected_digest) is None:
        raise FleetPreflightError("expected digest must use sha256:<64 lowercase hex>")

    digests = {snapshot.configuration_digest for snapshot in snapshots}
    configuration_digest = next(iter(digests)) if len(digests) == 1 else None
    digest_matches = expected_digest is None or configuration_digest == expected_digest
    if len(digests) != 1 or not digest_matches:
        status: FleetStatus = "DRIFT"
    elif any(
        snapshot.status != "READY" and not (allow_warning and snapshot.status == "WARNING")
        for snapshot in snapshots
    ):
        status = "NOT_READY"
    else:
        status = "READY"
    return FleetReport(
        status=status,
        configuration_digest=configuration_digest,
        expected_digest=expected_digest,
        instances=tuple(sorted(snapshots, key=lambda snapshot: snapshot.name)),
    )


def audit_fleet(
    instances: Sequence[FleetInstance],
    *,
    token: str,
    timeout_seconds: float,
    ca_file: Path | None = None,
    expected_digest: str | None = None,
    allow_warning: bool = False,
    fetch: Callable[..., InstanceReadiness] = fetch_instance_readiness,
) -> FleetReport:
    snapshots = tuple(
        fetch(
            instance,
            token=token,
            timeout_seconds=timeout_seconds,
            ca_file=ca_file,
        )
        for instance in instances
    )
    return evaluate_fleet(
        snapshots,
        expected_digest=expected_digest,
        allow_warning=allow_warning,
    )


def safe_fleet_report(report: FleetReport) -> dict[str, object]:
    return {
        "status": report.status,
        "configuration_digest": report.configuration_digest,
        "expected_digest": report.expected_digest,
        "instance_count": len(report.instances),
        "instances": [
            {
                "name": snapshot.name,
                "status": snapshot.status,
                "configuration_digest": snapshot.configuration_digest,
                "generated_at": snapshot.generated_at,
                "configured_key_count": snapshot.configured_key_count,
                "accepting_key_count": snapshot.accepting_key_count,
                "overlap_ready": snapshot.overlap_ready,
                "legacy_hmac_enabled": snapshot.legacy_hmac_enabled,
                "warnings": list(snapshot.warnings),
            }
            for snapshot in report.instances
        ],
    }


def fleet_exit_code(status: FleetStatus) -> int:
    if status == "READY":
        return 0
    if status == "NOT_READY":
        return 2
    return 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Query direct TestOps API instances and fail closed unless every instance is ready "
            "with the same verifier keyring digest. READY exits 0, NOT_READY exits 2, "
            "DRIFT exits 3, and validation/transport failures exit 4."
        )
    )
    parser.add_argument(
        "--instance",
        action="append",
        required=True,
        metavar="NAME=ORIGIN",
        help="Direct API instance origin. Repeat for at least two distinct instances.",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        required=True,
        help="File containing a short-lived System Admin bearer token.",
    )
    parser.add_argument(
        "--expected-digest",
        help="Optional approved sha256 keyring digest that every instance must match.",
    )
    parser.add_argument(
        "--ca-file",
        type=Path,
        help="Optional PEM CA bundle for the direct HTTPS instance endpoints.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=10.0,
        help="Per-instance request timeout from 1 to 60 seconds (default: 10).",
    )
    parser.add_argument(
        "--allow-warning",
        action="store_true",
        help="Treat WARNING instances as successful while still requiring one common digest.",
    )
    parser.add_argument(
        "--allow-http",
        action="store_true",
        help="Allow direct HTTP origins only on loopback for isolated development.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not 1 <= args.timeout_seconds <= 60:
            raise FleetPreflightError("timeout must be between 1 and 60 seconds")
        instances = parse_instances(args.instance, allow_http=args.allow_http)
        token = load_bearer_token(args.token_file)
        report = audit_fleet(
            instances,
            token=token,
            timeout_seconds=args.timeout_seconds,
            ca_file=args.ca_file,
            expected_digest=args.expected_digest,
            allow_warning=args.allow_warning,
        )
    except FleetPreflightError as exc:
        error = {"status": "INVALID", "error": "fleet preflight failed"}
        if exc.instance is not None:
            error["instance"] = exc.instance
        print(json.dumps(error, separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return 4

    print(json.dumps(safe_fleet_report(report), separators=(",", ":"), sort_keys=True))
    return fleet_exit_code(report.status)


if __name__ == "__main__":
    raise SystemExit(main())
