from __future__ import annotations

import json
import unittest
from pathlib import Path

from testops.contracts.schema_export import schemas

ROOT = Path(__file__).resolve().parents[2]


class SchemaArtifactTests(unittest.TestCase):
    def test_checked_in_schemas_match_contract_models(self) -> None:
        destination = ROOT / "packages/contracts/schemas"
        for filename, schema in schemas().items():
            expected = json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
            self.assertEqual(
                (destination / filename).read_text("utf-8"),
                expected,
                f"stale generated schema: {filename}",
            )


if __name__ == "__main__":
    unittest.main()
