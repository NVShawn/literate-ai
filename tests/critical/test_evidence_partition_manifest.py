from __future__ import annotations

import hashlib
import unittest
from types import SimpleNamespace

from literate_ai.source_to_specification import (
    EvidenceReference,
    SourceFileClassification,
    SourceInventory,
    SourceInventoryEntry,
    SourceToSpecificationError,
    build_evidence_partition_manifest,
    canonical_digest,
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


if __name__ == "__main__":
    unittest.main()
