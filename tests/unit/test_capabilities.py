from __future__ import annotations

import unittest

from testops.contracts import ASSERTIONS, OPERATIONS, get_assertion_spec, get_operation_spec


class CapabilityRegistryTests(unittest.TestCase):
    def test_registry_covers_current_legacy_executor(self) -> None:
        operations = {spec.key for spec in OPERATIONS}
        assertions = {spec.key for spec in ASSERTIONS}

        self.assertEqual(
            operations,
            {
                "input",
                "input_enter",
                "click",
                "select",
                "verify",
                "hover",
                "scroll",
                "wait",
                "nav",
                "find_click",
                "upload",
                "daterange",
                "switch_tab",
                "retry_report",
            },
        )
        self.assertTrue({"value_equals", "text_hidden", "element_disabled"}.issubset(assertions))

    def test_aliases_resolve_to_canonical_key(self) -> None:
        self.assertEqual(get_operation_spec("date_range").key, "daterange")
        self.assertEqual(get_assertion_spec("visible_text").key, "text_visible")

    def test_unknown_capability_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported operation"):
            get_operation_spec("magic_click")


if __name__ == "__main__":
    unittest.main()
