from __future__ import annotations

import hashlib
import hmac
import unittest
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from testops.api.config import (
    Settings,
    SupplyChainVerifierCredential,
    SupplyChainVerifierPublicKey,
)
from testops.api.schemas import AutomationPackageSupplyChainVerificationCreate
from testops.api.supply_chain_auth import (
    SupplyChainVerifierAuth,
    SupplyChainVerifierUnavailable,
    supply_chain_envelope_signature_base,
)

DIGEST = "sha256:" + "a" * 64


def report_payload(*, outcome: str = "VERIFIED") -> dict[str, object]:
    verified = outcome == "VERIFIED"
    return {
        "outcome": outcome,
        "policy_version": "policy-v1",
        "verifier": "trusted-verifier",
        "image_digest": DIGEST,
        "signature_bundle_digest": "sha256:" + "b" * 64,
        "provenance_digest": "sha256:" + "c" * 64,
        "sbom_digest": "sha256:" + "d" * 64,
        "signature_verified": verified,
        "transparency_log_verified": verified,
        "provenance_verified": verified,
        "sbom_verified": verified,
        "certificate_issuer": "https://issuer.example.invalid",
        "certificate_identity": "https://identity.example.invalid/workflow",
        "builder_id": "https://builder.example.invalid/v1",
        "source_repository": "https://git.example.invalid/testops",
        "source_revision": "e" * 40,
        "reason": None if verified else "policy rejected the evidence",
    }


class SupplyChainAdmissionTests(unittest.TestCase):
    def test_verified_supply_chain_report_requires_every_check(self) -> None:
        payload = report_payload()
        payload["sbom_verified"] = False

        with self.assertRaisesRegex(ValidationError, "every check"):
            AutomationPackageSupplyChainVerificationCreate.model_validate(payload)

    def test_rejected_supply_chain_report_requires_a_reason(self) -> None:
        payload = report_payload(outcome="REJECTED")
        payload["reason"] = None

        with self.assertRaisesRegex(ValidationError, "requires a reason"):
            AutomationPackageSupplyChainVerificationCreate.model_validate(payload)

    def test_supply_chain_policy_allowlists_are_exact_and_fail_closed_by_default(
        self,
    ) -> None:
        names = {
            "SUPPLY_CHAIN_ALLOWED_VERIFIERS": " verifier-a,verifier-b ",
            "SUPPLY_CHAIN_ALLOWED_CERTIFICATE_ISSUERS": "https://issuer.example.invalid",
            "SUPPLY_CHAIN_ALLOWED_CERTIFICATE_IDENTITIES": "identity-a,identity-b",
            "SUPPLY_CHAIN_ALLOWED_BUILDER_IDS": "builder-a",
            "SUPPLY_CHAIN_ALLOWED_SOURCE_REPOSITORIES": "repo-a",
            "SUPPLY_CHAIN_VERIFIER_ED25519_KEYS": (
                '[{"credential_id":"verifier-a/ed25519-current",'
                '"workload_identity":"spiffe://ci.example.invalid/testops/verifier-a",'
                '"public_key":"11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"}]'
            ),
            "SUPPLY_CHAIN_ALLOW_LEGACY_HMAC": "true",
            "SUPPLY_CHAIN_VERIFIER_HMAC_KEYS": (
                "verifier-a/current=0123456789abcdef0123456789abcdef,"
                "verifier-a/previous=fedcba9876543210fedcba9876543210"
            ),
        }
        with patch.dict("os.environ", {}, clear=True):
            defaults = Settings.from_environment()
        self.assertEqual(defaults.supply_chain_allowed_verifiers, ())
        self.assertEqual(defaults.supply_chain_allowed_certificate_identities, ())
        self.assertEqual(defaults.supply_chain_verifier_public_keys, ())
        self.assertEqual(defaults.supply_chain_verifier_credentials, ())
        self.assertFalse(defaults.supply_chain_allow_legacy_hmac)

        with patch.dict("os.environ", names, clear=True):
            configured = Settings.from_environment()
        self.assertEqual(
            configured.supply_chain_allowed_verifiers,
            ("verifier-a", "verifier-b"),
        )
        self.assertEqual(
            configured.supply_chain_allowed_certificate_issuers,
            ("https://issuer.example.invalid",),
        )
        self.assertEqual(
            configured.supply_chain_allowed_certificate_identities,
            ("identity-a", "identity-b"),
        )
        self.assertEqual(configured.supply_chain_allowed_builder_ids, ("builder-a",))
        self.assertEqual(
            configured.supply_chain_allowed_source_repositories,
            ("repo-a",),
        )
        self.assertEqual(
            [item.credential_id for item in configured.supply_chain_verifier_credentials],
            ["verifier-a/current", "verifier-a/previous"],
        )
        self.assertEqual(
            [item.credential_id for item in configured.supply_chain_verifier_public_keys],
            ["verifier-a/ed25519-current"],
        )
        self.assertEqual(
            configured.supply_chain_verifier_public_keys[0].workload_identity,
            "spiffe://ci.example.invalid/testops/verifier-a",
        )
        self.assertTrue(
            configured.supply_chain_verifier_public_keys[0].key_fingerprint.startswith("sha256:")
        )
        self.assertTrue(configured.supply_chain_allow_legacy_hmac)
        self.assertNotIn(
            "0123456789abcdef0123456789abcdef",
            repr(configured),
        )
        self.assertNotIn("d75a9801", repr(configured))

    def test_supply_chain_verifier_credentials_reject_weak_or_duplicate_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 32 bytes"):
            SupplyChainVerifierCredential(
                credential_id="trusted-verifier/current",
                verifier="trusted-verifier",
                secret="too-short",
            )
        with (
            patch.dict(
                "os.environ",
                {
                    "SUPPLY_CHAIN_VERIFIER_HMAC_KEYS": (
                        "trusted-verifier/current=0123456789abcdef0123456789abcdef,"
                        "trusted-verifier/current=fedcba9876543210fedcba9876543210"
                    )
                },
                clear=True,
            ),
            self.assertRaisesRegex(ValueError, "duplicate credential id"),
        ):
            Settings.from_environment()

    def test_supply_chain_ed25519_keys_reject_invalid_identity_key_and_duplicates(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS or SPIFFE"):
            SupplyChainVerifierPublicKey(
                credential_id="trusted-verifier/current",
                verifier="trusted-verifier",
                workload_identity="github-actions:release",
                public_key=b"k" * 32,
            )
        with self.assertRaisesRegex(ValueError, "exactly 32 bytes"):
            SupplyChainVerifierPublicKey(
                credential_id="trusted-verifier/current",
                verifier="trusted-verifier",
                workload_identity="spiffe://ci.example.invalid/testops/verifier",
                public_key=b"too-short",
            )
        duplicate = (
            '[{"credential_id":"trusted-verifier/current",'
            '"workload_identity":"spiffe://ci.example.invalid/testops/verifier",'
            '"public_key":"11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"},'
            '{"credential_id":"trusted-verifier/current",'
            '"workload_identity":"spiffe://ci.example.invalid/testops/verifier",'
            '"public_key":"11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"}]'
        )
        with (
            patch.dict(
                "os.environ",
                {"SUPPLY_CHAIN_VERIFIER_ED25519_KEYS": duplicate},
                clear=True,
            ),
            self.assertRaisesRegex(ValueError, "duplicate credential id"),
        ):
            Settings.from_environment()

    def test_legacy_hmac_requires_explicit_migration_switch(self) -> None:
        secret = "0123456789abcdef0123456789abcdef"
        credential_id = "trusted-verifier/legacy"

        def client(*, allow_legacy: bool) -> TestClient:
            app = FastAPI()
            app.state.settings = Settings(
                supply_chain_verifier_credentials=(
                    SupplyChainVerifierCredential(
                        credential_id=credential_id,
                        verifier="trusted-verifier",
                        secret=secret,
                    ),
                ),
                supply_chain_allow_legacy_hmac=allow_legacy,
            )

            @app.post("/verify")
            async def verify(principal: SupplyChainVerifierAuth) -> dict[str, str]:
                return {
                    "verifier": principal.verifier,
                    "algorithm": principal.signature_algorithm,
                }

            return TestClient(app)

        body = b'{"ok":true}'
        created = str(int(datetime.now(UTC).timestamp()))
        nonce = str(uuid4())
        request_digest = "sha256:" + hashlib.sha256(body).hexdigest()
        signature_base = supply_chain_envelope_signature_base(
            method="POST",
            path="/verify",
            created=created,
            nonce=nonce,
            request_digest=request_digest,
        )
        signature = hmac.new(secret.encode(), signature_base, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-TestOps-Verifier-Key-Id": credential_id,
            "X-TestOps-Envelope-Created": created,
            "X-TestOps-Envelope-Nonce": nonce,
            "X-TestOps-Envelope-Signature": f"hmac-sha256={signature}",
        }

        enabled = client(allow_legacy=True).post("/verify", content=body, headers=headers)
        self.assertEqual(enabled.status_code, 200, enabled.text)
        self.assertEqual(enabled.json()["algorithm"], "HMAC-SHA256")

        with self.assertRaises(SupplyChainVerifierUnavailable):
            client(allow_legacy=False).post("/verify", content=body, headers=headers)

        with (
            patch.dict(
                "os.environ",
                {
                    "SUPPLY_CHAIN_VERIFIER_ED25519_KEYS": (
                        '[{"credential_id":"trusted-verifier/current",'
                        '"workload_identity":"spiffe://ci.example.invalid/testops/verifier",'
                        '"public_key":"11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"}]'
                    ),
                    "SUPPLY_CHAIN_VERIFIER_HMAC_KEYS": (
                        "trusted-verifier/current=0123456789abcdef0123456789abcdef"
                    ),
                },
                clear=True,
            ),
            self.assertRaisesRegex(ValueError, "unique across keyrings"),
        ):
            Settings.from_environment()
