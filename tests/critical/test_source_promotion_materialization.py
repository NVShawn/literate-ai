from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.source_to_specification.promotion_materialization import (
    GenerationInputAudit,
    PromotionInputKind,
    SourcePromotionError,
    SourcePromotionInput,
    SourcePromotionMaterializer,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


def content_identity(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


class SourcePromotionMaterializerTests(unittest.TestCase):
    def test_unlisted_source_prompt_and_journal_canaries_never_enter_closure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = root / "accepted"
            accepted.mkdir()
            specification = b"# Portable greeter\n\nPrint exactly hello.\n"
            component = b'{"schema":"component","name":"greeter"}\n'
            (accepted / "spec.md").write_bytes(specification)
            (accepted / "component.json").write_bytes(component)
            (accepted / "baseline").mkdir()
            (accepted / "baseline" / "source.txt").write_text("SOURCE-CANARY")
            (accepted / "provenance").mkdir()
            (accepted / "provenance" / "prompt.txt").write_text("PROMPT-CANARY")
            (accepted / ".literate").mkdir()
            journal = accepted / ".literate" / "source-translation.json"
            journal.write_text(
                '{"schema":"legacy","journal":"JOURNAL-CANARY"}',
                encoding="utf-8",
            )
            inputs = (
                SourcePromotionInput(
                    kind=PromotionInputKind.COMPONENT_INTENT,
                    source_root=accepted,
                    source_root_label="accepted",
                    source_path="component.json",
                    target_path="component.json",
                    expected_content_identity=content_identity(component),
                ),
                SourcePromotionInput(
                    kind=PromotionInputKind.SPECIFICATION,
                    source_root=accepted,
                    source_root_label="accepted",
                    source_path="spec.md",
                    target_path="specs/behavior.md",
                    expected_content_identity=content_identity(specification),
                ),
            )

            first = SourcePromotionMaterializer().materialize(inputs, root / "first")
            serialized = json.dumps(first.audit.to_dict(), sort_keys=True)

            self.assertEqual(
                sorted(
                    path.relative_to(first.output_root).as_posix()
                    for path in first.output_root.rglob("*")
                    if path.is_file()
                ),
                ["component.json", "specs/behavior.md"],
            )
            for canary in ("SOURCE-CANARY", "PROMPT-CANARY", "JOURNAL-CANARY"):
                self.assertNotIn(canary, serialized)
                self.assertFalse(
                    any(
                        canary.encode() in path.read_bytes()
                        for path in first.output_root.rglob("*")
                        if path.is_file()
                    )
                )
            self.assertEqual(
                GenerationInputAudit.from_dict(first.audit.to_dict()), first.audit
            )
            SchemaCatalog().validate(
                "urn:literate-ai:schema:v2:generation-input-audit",
                first.audit.to_dict(),
            )

            # Non-authority history is invisible to both closure identities.
            journal.write_text(
                '{"schema":"legacy","journal":"A-DIFFERENT-JOURNAL-CANARY"}',
                encoding="utf-8",
            )
            (accepted / "baseline" / "source.txt").write_text("OTHER-SOURCE-CANARY")
            second = SourcePromotionMaterializer().materialize(inputs, root / "second")
            self.assertEqual(first.audit.identity, second.audit.identity)
            self.assertEqual(
                first.audit.materialized_tree_identity,
                second.audit.materialized_tree_identity,
            )

            # An admitted specification edit changes both identities.
            revised = specification + b"Exit successfully.\n"
            (accepted / "spec.md").write_bytes(revised)
            revised_inputs = (
                inputs[0],
                SourcePromotionInput(
                    kind=PromotionInputKind.SPECIFICATION,
                    source_root=accepted,
                    source_root_label="accepted",
                    source_path="spec.md",
                    target_path="specs/behavior.md",
                    expected_content_identity=content_identity(revised),
                ),
            )
            third = SourcePromotionMaterializer().materialize(
                revised_inputs, root / "third"
            )
            self.assertNotEqual(first.audit.identity, third.audit.identity)
            self.assertNotEqual(
                first.audit.materialized_tree_identity,
                third.audit.materialized_tree_identity,
            )

    def test_rejects_traversal_forbidden_targets_and_overlapping_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = root / "accepted"
            accepted.mkdir()
            content = b"spec"
            (accepted / "spec.md").write_bytes(content)

            with self.assertRaises(SourcePromotionError) as traversal:
                SourcePromotionInput(
                    kind=PromotionInputKind.SPECIFICATION,
                    source_root=accepted,
                    source_root_label="accepted",
                    source_path="../source.py",
                    target_path="spec.md",
                    expected_content_identity=content_identity(content),
                )
            self.assertEqual(traversal.exception.code, "promotion.invalid_path")

            with self.assertRaises(SourcePromotionError) as forbidden:
                SourcePromotionInput(
                    kind=PromotionInputKind.SPECIFICATION,
                    source_root=accepted,
                    source_root_label="accepted",
                    source_path="spec.md",
                    target_path=".literate/source-translation.json",
                    expected_content_identity=content_identity(content),
                )
            self.assertEqual(forbidden.exception.code, "promotion.forbidden_target")

            item = SourcePromotionInput(
                kind=PromotionInputKind.SPECIFICATION,
                source_root=accepted,
                source_root_label="accepted",
                source_path="spec.md",
                target_path="spec.md",
                expected_content_identity=content_identity(content),
            )
            with self.assertRaises(SourcePromotionError) as overlap:
                SourcePromotionMaterializer().materialize(
                    (item,), accepted / "component"
                )
            self.assertEqual(overlap.exception.code, "promotion.path_overlap")


if __name__ == "__main__":
    unittest.main()
