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
                "set_viewport",
                "wait_hidden",
                "vision_compare_step",
            },
        )
        self.assertTrue(
            {
                "value_equals",
                "text_hidden",
                "element_disabled",
                "vision_contains",
                "vision_count",
                "vision_page_state",
                "vision_canvas_ready",
                "vision_compare_reference",
                "vision_step_sequence",
            }.issubset(assertions)
        )

    def test_visual_capabilities_keep_excel_argument_contracts(self) -> None:
        viewport = get_operation_spec("set_viewport")
        hidden = get_operation_spec("wait_hidden")
        compare_step = get_operation_spec("vision_compare_step")

        self.assertTrue(viewport.requires_locator)
        self.assertTrue(hidden.requires_locator)
        self.assertTrue(compare_step.requires_locator)
        self.assertTrue(compare_step.consumes_input)
        self.assertTrue(compare_step.requires_input)
        self.assertFalse(get_assertion_spec("vision_step_sequence").requires_locator)

    def test_aliases_resolve_to_canonical_key(self) -> None:
        self.assertEqual(get_operation_spec("date_range").key, "daterange")
        self.assertEqual(get_assertion_spec("visible_text").key, "text_visible")

    def test_unknown_capability_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported operation"):
            get_operation_spec("magic_click")


if __name__ == "__main__":
    unittest.main()
