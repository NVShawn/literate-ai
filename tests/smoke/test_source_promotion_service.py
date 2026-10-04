from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from literate_ai.application import SourcePromotionService
from literate_ai.source_to_specification import (
    PromotionInputKind,
    SourcePromotionInput,
    inventory_source,
)


class SourcePromotionServiceTests(unittest.TestCase):
    def test_public_service_materializes_and_assesses_the_exact_audited_closure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            accepted = root / "accepted"
            source.mkdir()
            accepted.mkdir()
            (source / "application.py").write_text(
                "SOURCE_CANARY = 'must-not-cross'\n", encoding="utf-8"
            )
            specification = b"# Contract\nEmit the documented result.\n"
            (accepted / "spec.md").write_bytes(specification)
            item = SourcePromotionInput(
                kind=PromotionInputKind.SPECIFICATION,
                source_root=accepted,
                source_root_label="accepted-specifications",
                source_path="spec.md",
                target_path="spec.md",
                expected_content_identity=(
                    "sha256:" + hashlib.sha256(specification).hexdigest()
                ),
            )

            service = SourcePromotionService()
            result = service.materialize((item,), root / "component")
            assessment = service.assess_source_exclusion(
                result.audit, inventory_source(source)
            )

            self.assertTrue(assessment.source_excluded)
            self.assertEqual(
                tuple(
                    path.name
                    for path in result.output_root.rglob("*")
                    if path.is_file()
                ),
                ("spec.md",),
            )
            self.assertNotIn(
                b"must-not-cross", (result.output_root / "spec.md").read_bytes()
            )


if __name__ == "__main__":
    unittest.main()
