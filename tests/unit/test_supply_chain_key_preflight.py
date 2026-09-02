from __future__ import annotations

import contextlib
import io
import json
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from scripts.check_supply_chain_key_readiness import (
    main,
    readiness_exit_code,
    safe_readiness_report,
)

from testops.api.config import Settings, SupplyChainVerifierPublicKey
from testops.api.supply_chain_operations import collect_supply_chain_key_readiness


def configured_settings() -> Settings:
    return Settings(
        supply_chain_verifier_public_keys=(
            SupplyChainVerifierPublicKey(
                credential_id="trusted-verifier/current",
                verifier="trusted-verifier",
                workload_identity="https://ci.example.invalid/workflows/release.yml",
                public_key=b"a" * 32,
            ),
        )
    )


class SupplyChainKeyPreflightTests(unittest.TestCase):
    def test_safe_report_omits_key_level_metadata(self) -> None:
        snapshot = collect_supply_chain_key_readiness(
            configured_settings(),
            generated_at=datetime(2026, 8, 24, tzinfo=UTC),
        )

        serialized = json.dumps(safe_readiness_report(snapshot), sort_keys=True)

        self.assertNotIn("keys", serialized)
        self.assertNotIn("trusted-verifier/current", serialized)
        self.assertNotIn("ci.example.invalid", serialized)
        self.assertRegex(
            safe_readiness_report(snapshot)["configuration_digest"],
            r"^sha256:[0-9a-f]{64}$",
        )

    def test_readiness_exit_codes_fail_closed_by_default(self) -> None:
        self.assertEqual(readiness_exit_code("READY"), 0)
        self.assertEqual(readiness_exit_code("WARNING"), 2)
        self.assertEqual(readiness_exit_code("BLOCKED"), 3)
        self.assertEqual(readiness_exit_code("WARNING", allow_warning=True), 0)

    def test_invalid_configuration_error_is_generic(self) -> None:
        stderr = io.StringIO()
        with (
            patch(
                "scripts.check_supply_chain_key_readiness.Settings.from_environment",
                side_effect=ValueError("secret-bearing-config-value"),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = main([])

        self.assertEqual(exit_code, 4)
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"status": "INVALID", "error": "configuration validation failed"},
        )
        self.assertNotIn("secret-bearing-config-value", stderr.getvalue())

    def test_warning_cli_is_nonzero_unless_explicitly_allowed(self) -> None:
        with patch(
            "scripts.check_supply_chain_key_readiness.Settings.from_environment",
            return_value=configured_settings(),
        ):
            default_stdout = io.StringIO()
            with contextlib.redirect_stdout(default_stdout):
                default_exit_code = main([])
            allowed_stdout = io.StringIO()
            with contextlib.redirect_stdout(allowed_stdout):
                allowed_exit_code = main(["--allow-warning"])

        self.assertEqual(default_exit_code, 2)
        self.assertEqual(allowed_exit_code, 0)
        default_report = json.loads(default_stdout.getvalue())
        allowed_report = json.loads(allowed_stdout.getvalue())
        self.assertEqual(default_report["status"], "WARNING")
        for report in (default_report, allowed_report):
            generated_at = report.pop("generated_at")
            self.assertEqual(datetime.fromisoformat(generated_at).utcoffset(), UTC.utcoffset(None))
        self.assertEqual(default_report, allowed_report)


if __name__ == "__main__":
    unittest.main()
