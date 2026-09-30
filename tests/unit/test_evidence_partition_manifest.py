from __future__ import annotations

import hashlib
import unittest
from types import SimpleNamespace

from literate_ai.source_to_specification import (
    EvidencePartitionManifest,
    EvidencePathDisposition,
    EvidenceReference,
    ModelEvidenceBatchPlan,
    SourceFileClassification,
    SourceInventory,
    SourceInventoryEntry,
    SourceToSpecificationError,
    build_evidence_partition_manifest,
    canonical_digest,
    plan_model_evidence_batches,
)


def entry(
    path: str, content: bytes, classification: SourceFileClassification
) -> SourceInventoryEntry:
    return SourceInventoryEntry(
        path,
        "sha256:" + hashlib.sha256(content).hexdigest(),
        len(content),
        classification,
        "python" if path.endswith(".py") else None,
    )


class EvidencePartitionManifestTests(unittest.TestCase):
    def test_every_inventory_path_gets_one_explicit_disposition(self) -> None:
        source = b"def main():\n    return 1\n"
        entries = (
            entry("app.py", source, SourceFileClassification.SOURCE),
            entry("package/__init__.py", b"", SourceFileClassification.SOURCE),
            entry(
                "missing.py", b"def missing(): pass\n", SourceFileClassification.SOURCE
            ),
            entry("secret.env", b"TOKEN=secret\n", SourceFileClassification.SENSITIVE),
            entry("asset.bin", b"\x00\x01", SourceFileClassification.BINARY),
        )
        inventory = SourceInventory(entries, ("vendor",))
        reference = EvidenceReference(
            "evidence:app",
            inventory.identity,
            entries[0].content_digest,
            "app.py",
            "main",
        )
        intelligence = SimpleNamespace(
            authority_source_snapshot_id=inventory.identity,
            provider_id="fixture-intelligence",
            provider_version="1.1.1",
            executable_identity=canonical_digest("fixture-intelligence"),
            evidence=(SimpleNamespace(reference=reference, content=source.decode()),),
        )

        manifest = build_evidence_partition_manifest(inventory, intelligence)
        by_path = {item.path: item for item in manifest.items}

        self.assertEqual(set(by_path), {item.path for item in entries})
        self.assertEqual(by_path["app.py"].disposition, EvidencePathDisposition.FULL)
        self.assertEqual(
            by_path["package/__init__.py"].disposition,
            EvidencePathDisposition.EMPTY,
        )
        self.assertEqual(
            by_path["missing.py"].disposition,
            EvidencePathDisposition.BLOCKING_OMITTED,
        )
        self.assertEqual(
            by_path["secret.env"].disposition,
            EvidencePathDisposition.SENSITIVE_EXCLUDED,
        )
        self.assertEqual(
            by_path["asset.bin"].disposition,
            EvidencePathDisposition.UNSUPPORTED,
        )
        self.assertEqual(manifest.excluded_directories, ("vendor",))
        self.assertEqual(
            EvidencePartitionManifest.from_dict(manifest.to_dict()), manifest
        )
        with self.assertRaisesRegex(
            SourceToSpecificationError, "omitted by the model budget"
        ):
            manifest.require_complete()

    def test_empty_source_is_explicitly_complete_without_model_evidence(self) -> None:
        empty = entry("tests/__init__.py", b"", SourceFileClassification.TEST)
        inventory = SourceInventory((empty,), ())
        intelligence = SimpleNamespace(
            authority_source_snapshot_id=inventory.identity,
            provider_id="fixture-intelligence",
            provider_version="1.1.1",
            executable_identity=canonical_digest("fixture-intelligence"),
            evidence=(),
        )

        manifest = build_evidence_partition_manifest(inventory, intelligence)

        self.assertEqual(manifest.items[0].disposition, EvidencePathDisposition.EMPTY)
        manifest.require_complete()

    def test_reordering_inventory_does_not_change_manifest(self) -> None:
        content = b"def main(): return 1\n"
        first_entry = entry("a.py", content, SourceFileClassification.SOURCE)
        second_entry = entry("b.txt", b"docs\n", SourceFileClassification.DOCUMENTATION)
        first = SourceInventory((first_entry, second_entry), ())
        second = SourceInventory((second_entry, first_entry), ())

        def intelligence(inventory: SourceInventory):
            evidence = tuple(
                SimpleNamespace(
                    reference=EvidenceReference(
                        f"evidence:{item.path}",
                        inventory.identity,
                        item.content_digest,
                        item.path,
                        "main" if item.language else "",
                    ),
                    content=(content if item.path == "a.py" else b"docs\n").decode(),
                )
                for item in inventory.entries
            )
            return SimpleNamespace(
                authority_source_snapshot_id=inventory.identity,
                provider_id="fixture-intelligence",
                provider_version="1.1.1",
                executable_identity=canonical_digest("fixture-intelligence"),
                evidence=evidence,
            )

        first_manifest = build_evidence_partition_manifest(first, intelligence(first))
        second_manifest = build_evidence_partition_manifest(
            second, intelligence(second)
        )

        # SourceInventory identity currently retains entry order, while path projection
        # and every disposition remain deterministic under catalog reordering.
        self.assertEqual(first_manifest.items, second_manifest.items)

    def test_duplicate_content_does_not_hide_missing_path_evidence(self) -> None:
        content = b"def main(): return 1\n"
        entries = (
            entry("a.py", content, SourceFileClassification.SOURCE),
            entry("b.py", content, SourceFileClassification.SOURCE),
        )
        inventory = SourceInventory(entries, ())
        reference = EvidenceReference(
            "evidence:a",
            inventory.identity,
            entries[0].content_digest,
            "a.py",
            "main",
        )
        intelligence = SimpleNamespace(
            authority_source_snapshot_id=inventory.identity,
            provider_id="fixture-intelligence",
            provider_version="1.1.1",
            executable_identity=canonical_digest("fixture-intelligence"),
            evidence=(SimpleNamespace(reference=reference, content=content.decode()),),
        )

        manifest = build_evidence_partition_manifest(inventory, intelligence)

        self.assertEqual(
            manifest.items[1].disposition,
            EvidencePathDisposition.BLOCKING_OMITTED,
        )

    def test_evidence_outside_inventory_or_for_sensitive_path_is_rejected(self) -> None:
        content = b"TOKEN=secret\n"
        secret = entry("secret.env", content, SourceFileClassification.SENSITIVE)
        inventory = SourceInventory((secret,), ())

        def intelligence(path: str):
            return SimpleNamespace(
                authority_source_snapshot_id=inventory.identity,
                provider_id="fixture-intelligence",
                provider_version="1.1.1",
                executable_identity=canonical_digest("fixture-intelligence"),
                evidence=(
                    SimpleNamespace(
                        reference=EvidenceReference(
                            "evidence:secret",
                            inventory.identity,
                            secret.content_digest,
                            path,
                            "",
                        ),
                        content=content.decode(),
                    ),
                ),
            )

        with self.assertRaisesRegex(
            SourceToSpecificationError, "outside the inventory"
        ):
            build_evidence_partition_manifest(inventory, intelligence("other.env"))
        with self.assertRaisesRegex(SourceToSpecificationError, "sensitive source"):
            build_evidence_partition_manifest(inventory, intelligence("secret.env"))

    def test_language_evidence_is_partitioned_once_under_exact_byte_budget(
        self,
    ) -> None:
        contents = ("aaaa", "bbbbbb", "ccc")
        evidence = tuple(
            SimpleNamespace(
                language="python",
                reference=SimpleNamespace(
                    path=f"{index}.py", evidence_id=f"evidence:{index}"
                ),
                content=content,
            )
            for index, content in enumerate(contents)
        )
        intelligence = SimpleNamespace(
            authority_source_snapshot_id=canonical_digest("source"),
            evidence=evidence,
        )

        plan = plan_model_evidence_batches(
            intelligence, languages=("python",), maximum_batch_bytes=10
        )

        self.assertEqual([item.admitted_byte_count for item in plan.batches], [10, 3])
        self.assertEqual(
            [item.evidence_ids for item in plan.batches],
            [("evidence:0", "evidence:1"), ("evidence:2",)],
        )
        self.assertEqual([item.language_ordinal for item in plan.batches], [0, 1])
        self.assertEqual(ModelEvidenceBatchPlan.from_dict(plan.to_dict()), plan)
        with self.assertRaisesRegex(SourceToSpecificationError, "exceeds"):
            plan_model_evidence_batches(
                intelligence, languages=("python",), maximum_batch_bytes=5
            )


if __name__ == "__main__":
    unittest.main()
