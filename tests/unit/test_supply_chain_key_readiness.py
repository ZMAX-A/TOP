from __future__ import annotations

import unittest
from datetime import UTC, datetime

from testops.api.config import (
    Settings,
    SupplyChainVerifierCredential,
    SupplyChainVerifierPublicKey,
)
from testops.api.schemas import SupplyChainVerifierKeyReadinessResponse
from testops.api.supply_chain_operations import (
    collect_supply_chain_key_readiness,
    supply_chain_keyring_configuration_digest,
)

NOW = datetime(2026, 8, 24, 8, tzinfo=UTC)


def key(
    key_id: str,
    public_key_byte: int,
    *,
    verifier: str = "trusted-verifier",
    workload_identity: str | None = None,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
    revoked_at: datetime | None = None,
) -> SupplyChainVerifierPublicKey:
    return SupplyChainVerifierPublicKey(
        credential_id=f"{verifier}/{key_id}",
        verifier=verifier,
        workload_identity=(
            workload_identity or f"https://ci.example.invalid/workflows/{verifier}.yml"
        ),
        public_key=bytes([public_key_byte]) * 32,
        not_before=not_before,
        not_after=not_after,
        revoked_at=revoked_at,
    )


class SupplyChainKeyReadinessTests(unittest.TestCase):
    def test_no_configured_ed25519_key_is_blocked(self) -> None:
        snapshot = collect_supply_chain_key_readiness(Settings(), generated_at=NOW)

        self.assertEqual(snapshot["status"], "BLOCKED")
        self.assertEqual(snapshot["accepting_key_count"], 0)
        self.assertEqual(snapshot["warnings"], ("NO_ED25519_KEYS",))

    def test_lifecycle_counts_and_expiry_warning_are_deterministic(self) -> None:
        settings = Settings(
            supply_chain_key_expiry_warning_days=30,
            supply_chain_allow_legacy_hmac=True,
            supply_chain_verifier_public_keys=(
                key("active", 1, not_after=datetime(2026, 10, 1, tzinfo=UTC)),
                key("expiring", 2, not_after=datetime(2026, 9, 1, tzinfo=UTC)),
                key("future", 3, not_before=datetime(2026, 8, 25, tzinfo=UTC)),
                key("expired", 4, not_after=datetime(2026, 8, 24, tzinfo=UTC)),
                key("revoked", 5, revoked_at=datetime(2026, 8, 23, tzinfo=UTC)),
            ),
        )

        snapshot = collect_supply_chain_key_readiness(settings, generated_at=NOW)
        validated = SupplyChainVerifierKeyReadinessResponse.model_validate(snapshot)

        self.assertEqual(validated.status, "WARNING")
        self.assertEqual(validated.accepting_key_count, 2)
        self.assertEqual(validated.future_key_count, 1)
        self.assertEqual(validated.expiring_soon_key_count, 1)
        self.assertEqual(validated.expired_key_count, 1)
        self.assertEqual(validated.revoked_key_count, 1)
        self.assertTrue(validated.overlap_ready)
        self.assertEqual(
            {item.credential_id: item.status for item in validated.keys},
            {
                "trusted-verifier/active": "ACTIVE",
                "trusted-verifier/expired": "EXPIRED",
                "trusted-verifier/expiring": "EXPIRING_SOON",
                "trusted-verifier/future": "NOT_YET_ACTIVE",
                "trusted-verifier/revoked": "REVOKED",
            },
        )
        self.assertEqual(
            validated.warnings,
            ("ACTIVE_KEY_EXPIRING_SOON", "LEGACY_HMAC_ENABLED"),
        )
        serialized = validated.model_dump(mode="json")
        self.assertNotIn("public_key", serialized["keys"][0])
        self.assertRegex(validated.configuration_digest, r"^sha256:[0-9a-f]{64}$")

    def test_configuration_digest_is_order_and_time_independent(self) -> None:
        current = key("current", 1)
        next_key = key(
            "next",
            2,
            not_before=datetime(2026, 8, 25, tzinfo=UTC),
            not_after=datetime(2026, 10, 1, tzinfo=UTC),
        )
        hmac_current = SupplyChainVerifierCredential(
            credential_id="trusted-verifier/legacy-a",
            verifier="trusted-verifier",
            secret="a" * 32,
        )
        hmac_next = SupplyChainVerifierCredential(
            credential_id="trusted-verifier/legacy-b",
            verifier="trusted-verifier",
            secret="b" * 32,
        )
        first = Settings(
            supply_chain_verifier_public_keys=(current, next_key),
            supply_chain_verifier_credentials=(hmac_current, hmac_next),
        )
        reordered = Settings(
            supply_chain_verifier_public_keys=(next_key, current),
            supply_chain_verifier_credentials=(hmac_next, hmac_current),
        )

        first_snapshot = collect_supply_chain_key_readiness(first, generated_at=NOW)
        later_snapshot = collect_supply_chain_key_readiness(
            reordered,
            generated_at=datetime(2026, 8, 26, tzinfo=UTC),
        )

        self.assertEqual(
            first_snapshot["configuration_digest"],
            later_snapshot["configuration_digest"],
        )

    def test_configuration_digest_changes_with_keyring_policy(self) -> None:
        baseline = Settings(supply_chain_verifier_public_keys=(key("current", 1),))
        changed_key = Settings(supply_chain_verifier_public_keys=(key("current", 2),))
        changed_warning_window = Settings(
            supply_chain_key_expiry_warning_days=31,
            supply_chain_verifier_public_keys=(key("current", 1),),
        )
        changed_lifecycle = Settings(
            supply_chain_verifier_public_keys=(
                key(
                    "current",
                    1,
                    not_after=datetime(2026, 10, 1, tzinfo=UTC),
                ),
            ),
        )

        baseline_digest = supply_chain_keyring_configuration_digest(baseline)

        self.assertNotEqual(
            baseline_digest,
            supply_chain_keyring_configuration_digest(changed_key),
        )
        self.assertNotEqual(
            baseline_digest,
            supply_chain_keyring_configuration_digest(changed_warning_window),
        )
        self.assertNotEqual(
            baseline_digest,
            supply_chain_keyring_configuration_digest(changed_lifecycle),
        )

    def test_configuration_digest_does_not_attest_hmac_secret(self) -> None:
        def settings_with_secret(secret: str) -> Settings:
            return Settings(
                supply_chain_allow_legacy_hmac=True,
                supply_chain_verifier_credentials=(
                    SupplyChainVerifierCredential(
                        credential_id="trusted-verifier/legacy",
                        verifier="trusted-verifier",
                        secret=secret,
                    ),
                ),
            )

        first = supply_chain_keyring_configuration_digest(settings_with_secret("a" * 32))
        second = supply_chain_keyring_configuration_digest(settings_with_secret("b" * 32))

        self.assertEqual(first, second)

    def test_two_accepting_keys_without_warnings_are_rotation_ready(self) -> None:
        settings = Settings(
            supply_chain_verifier_public_keys=(key("current", 1), key("next", 2)),
        )

        snapshot = collect_supply_chain_key_readiness(settings, generated_at=NOW)

        self.assertEqual(snapshot["status"], "READY")
        self.assertTrue(snapshot["overlap_ready"])
        self.assertEqual(snapshot["warnings"], ())

    def test_single_accepting_key_requires_overlap(self) -> None:
        snapshot = collect_supply_chain_key_readiness(
            Settings(supply_chain_verifier_public_keys=(key("current", 1),)),
            generated_at=NOW,
        )

        self.assertEqual(snapshot["status"], "WARNING")
        self.assertEqual(snapshot["warnings"], ("NO_OVERLAPPING_ACTIVE_KEYS",))

    def test_keys_for_different_identities_do_not_form_a_rotation_overlap(self) -> None:
        snapshot = collect_supply_chain_key_readiness(
            Settings(
                supply_chain_verifier_public_keys=(
                    key("current", 1, verifier="verifier-a"),
                    key("current", 2, verifier="verifier-b"),
                )
            ),
            generated_at=NOW,
        )

        self.assertEqual(snapshot["accepting_key_count"], 2)
        self.assertFalse(snapshot["overlap_ready"])
        self.assertEqual(snapshot["status"], "WARNING")
        self.assertEqual(snapshot["warnings"], ("NO_OVERLAPPING_ACTIVE_KEYS",))


if __name__ == "__main__":
    unittest.main()
