from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from testops.contracts import CaseBaseline
from testops.migrations.legacy_excel import (
    LegacyExcelMigrationError,
    canonical_json_bytes,
    migrate_legacy_excel,
    write_migration_result,
)

ROOT = Path(__file__).resolve().parents[2]
BASELINE_DIR = ROOT / "baselines/yanjia-ai-web/case-v1.0.0"
HARDENED_BASELINE_DIR = ROOT / "baselines/yanjia-ai-web/case-v1.0.1"
LEGACY_SOURCE = ROOT.parent / "web/test_cases/test_case.xlsx"


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class LegacyExcelMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.baseline_bytes = (BASELINE_DIR / "case-baseline.json").read_bytes()
        cls.baseline = CaseBaseline.model_validate_json(cls.baseline_bytes)
        cls.audit = json.loads((BASELINE_DIR / "migration-audit.json").read_text("utf-8"))
        cls.manifest = json.loads((BASELINE_DIR / "manifest.json").read_text("utf-8"))

    def test_checked_in_baseline_matches_manifest_and_expected_counts(self) -> None:
        self.assertEqual(self.manifest["baseline"]["digest"], _sha256(self.baseline_bytes))
        self.assertEqual(self.manifest["baseline"]["case_count"], 92)
        self.assertEqual(self.manifest["baseline"]["enabled_case_count"], 89)
        self.assertEqual(self.audit["counts"]["disabled_cases"], 3)
        self.assertEqual(self.audit["counts"]["errors"], 0)
        self.assertEqual(len({case.case_id for case in self.baseline.cases}), 92)

    def test_legacy_edge_values_are_preserved_or_explicitly_normalized(self) -> None:
        cases = {case.case_code: case for case in self.baseline.cases}
        numeric_input = cases["TC-HOME-003"].steps[0].input
        space_input = cases["TC-DETAIL-032"].steps[15].input

        self.assertEqual(numeric_input, "13626710399")
        self.assertIsInstance(numeric_input, str)
        self.assertEqual(space_input, " ")
        self.assertEqual(cases["TC-CUSTOMER-022"].priority.value, "P2")
        self.assertFalse(cases["TC-DETAIL-025"].enabled)
        self.assertEqual(cases["TC-DETAIL-030"].steps[12].operation, "retry_report")

    def test_audit_exposes_legacy_dead_input_slots_without_values(self) -> None:
        warnings = self.audit["warnings"]
        self.assertEqual(len(warnings), 8)
        self.assertEqual({warning["code"] for warning in warnings}, {"unused_input_segments"})
        self.assertTrue(all("content_digest" in warning for warning in warnings))
        self.assertTrue(all("value" not in warning for warning in warnings))

    def test_hardened_baseline_changes_only_successful_login_assertion(self) -> None:
        hardened_bytes = (HARDENED_BASELINE_DIR / "case-baseline.json").read_bytes()
        hardened = CaseBaseline.model_validate_json(hardened_bytes)
        hardened_manifest = json.loads((HARDENED_BASELINE_DIR / "manifest.json").read_text("utf-8"))
        self.assertEqual(hardened_manifest["baseline"]["digest"], _sha256(hardened_bytes))
        self.assertEqual(hardened.source.kind, "derived_baseline")
        self.assertEqual(hardened.source.parent_baseline_id, self.baseline.baseline_id)

        parent_cases = {case.case_code: case for case in self.baseline.cases}
        hardened_cases = {case.case_code: case for case in hardened.cases}
        changed_codes = {
            code
            for code in parent_cases
            if parent_cases[code].model_dump() != hardened_cases[code].model_dump()
        }
        self.assertEqual(changed_codes, {"TC-LOGIN-007"})
        self.assertEqual(hardened_cases["TC-LOGIN-007"].assertion.type, "url_not_contains")
        self.assertEqual(
            hardened_cases["TC-LOGIN-007"].assertion.expected,
            "URL不包含'/login'",
        )

    @unittest.skipUnless(LEGACY_SOURCE.exists(), "legacy workbook is outside the standalone repo")
    def test_source_regeneration_is_byte_for_byte_deterministic(self) -> None:
        result = migrate_legacy_excel(
            LEGACY_SOURCE,
            project_key="yanjia-ai-web",
            version="case-v1.0.0",
        )
        self.assertEqual(canonical_json_bytes(result.baseline), self.baseline_bytes)
        self.assertEqual(
            canonical_json_bytes(result.audit),
            (BASELINE_DIR / "migration-audit.json").read_bytes(),
        )

    @unittest.skipUnless(LEGACY_SOURCE.exists(), "legacy workbook is outside the standalone repo")
    def test_published_baseline_refuses_changed_content(self) -> None:
        result = migrate_legacy_excel(
            LEGACY_SOURCE,
            project_key="yanjia-ai-web",
            version="case-v1.0.0",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "case-v1.0.0"
            paths = write_migration_result(result, destination)
            paths["baseline"].write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(LegacyExcelMigrationError, "拒绝覆盖"):
                write_migration_result(result, destination)


if __name__ == "__main__":
    unittest.main()
