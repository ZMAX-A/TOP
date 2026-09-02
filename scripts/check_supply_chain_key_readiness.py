"""Fail-closed deployment preflight for supply-chain verifier key readiness."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api/src"))

from testops.api.config import Settings  # noqa: E402
from testops.api.supply_chain_operations import (  # noqa: E402
    SupplyChainKeyReadinessSnapshot,
    VerifierKeyReadinessStatus,
    collect_supply_chain_key_readiness,
)


def safe_readiness_report(snapshot: SupplyChainKeyReadinessSnapshot) -> dict[str, object]:
    """Return a deployment report without key identities or fingerprints."""

    return {
        "status": snapshot["status"],
        "configuration_digest": snapshot["configuration_digest"],
        "generated_at": snapshot["generated_at"].isoformat(),
        "expiry_warning_days": snapshot["expiry_warning_days"],
        "configured_key_count": snapshot["configured_key_count"],
        "accepting_key_count": snapshot["accepting_key_count"],
        "future_key_count": snapshot["future_key_count"],
        "expiring_soon_key_count": snapshot["expiring_soon_key_count"],
        "expired_key_count": snapshot["expired_key_count"],
        "revoked_key_count": snapshot["revoked_key_count"],
        "overlap_ready": snapshot["overlap_ready"],
        "legacy_hmac_enabled": snapshot["legacy_hmac_enabled"],
        "warnings": list(snapshot["warnings"]),
    }


def readiness_exit_code(
    status: VerifierKeyReadinessStatus,
    *,
    allow_warning: bool = False,
) -> int:
    if status == "READY" or (status == "WARNING" and allow_warning):
        return 0
    if status == "WARNING":
        return 2
    return 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate supply-chain verifier key readiness from the current environment. "
            "READY exits 0, WARNING exits 2, BLOCKED exits 3, and invalid configuration exits 4."
        )
    )
    parser.add_argument(
        "--allow-warning",
        action="store_true",
        help="Treat WARNING as a successful preflight while still reporting its warnings.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = Settings.from_environment()
        snapshot = collect_supply_chain_key_readiness(settings)
    except Exception:  # noqa: BLE001 - CLI boundary must not print secret-bearing config errors.
        print(
            json.dumps(
                {"status": "INVALID", "error": "configuration validation failed"},
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 4

    print(json.dumps(safe_readiness_report(snapshot), separators=(",", ":"), sort_keys=True))
    return readiness_exit_code(snapshot["status"], allow_warning=args.allow_warning)


if __name__ == "__main__":
    raise SystemExit(main())
