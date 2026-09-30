from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.builders.materialization import (
    ArtifactMaterializationError,
    materialize_artifact_plan,
)
from literate_ai.contracts import (
    ArtifactMaterializationPlan,
    BlobRef,
    ContentIdentity,
    SourceTreeEntry,
    SourceTreeEntryOrigin,
)


def _identity(label: str) -> ContentIdentity:
    return ContentIdentity.parse_uri(
        "sha256:" + hashlib.sha256(label.encode()).hexdigest()
    )


def _blob(content: bytes, media_type: str) -> BlobRef:
    return BlobRef(
        digest=hashlib.sha256(content).hexdigest(),
        size=len(content),
        media_type=media_type,
    )


class ArtifactMaterializationAdapterTests(unittest.TestCase):
    def test_refuses_unhandled_sdk_inputs_before_creating_an_execution_root(self):
        plan = ArtifactMaterializationPlan(
            _identity("tree"),
            _identity("root"),
            (),
            native_sdk_input_identities=(_identity("sdk-input"),),
        )
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "execution"
            with self.assertRaisesRegex(ArtifactMaterializationError, "native SDK"):
                materialize_artifact_plan(
                    plan, destination, read_blob=lambda _blob: self.fail("blob read")
                )
            self.assertFalse(destination.exists())

    def test_materializes_every_exact_blob_into_a_new_root(self) -> None:
        values = {
            "src/main.py": (b"print('ok')\n", "text/x-python"),
            "assets/table.bin": (b"\x00\x01\xff", "application/octet-stream"),
        }
        blobs = {
            _blob(content, media_type): content
            for content, media_type in values.values()
        }
        entries = tuple(
            sorted(
                (
                    SourceTreeEntry(
                        path,
                        "generated-source" if path.endswith(".py") else "data",
                        (
                            SourceTreeEntryOrigin.GENERATED_TEXT
                            if path.endswith(".py")
                            else SourceTreeEntryOrigin.AUTHORED_BINARY
                        ),
                        blob,
                    )
                    for path, (content, media_type) in values.items()
                    for blob in (_blob(content, media_type),)
                ),
                key=lambda item: item.path,
            )
        )
        plan = ArtifactMaterializationPlan(
            _identity("tree"), _identity("root"), entries
        )
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "execution"
            result = materialize_artifact_plan(
                plan, destination, read_blob=blobs.__getitem__
            )
            self.assertEqual(result.paths, tuple(item.path for item in entries))
            self.assertEqual(
                (destination / "assets/table.bin").read_bytes(), b"\x00\x01\xff"
            )
            self.assertEqual(result.plan_identity, plan.identity)

    def test_refuses_existing_root_and_removes_failed_fresh_root(self) -> None:
        content = b"expected"
        blob = _blob(content, "text/plain")
        plan = ArtifactMaterializationPlan(
            _identity("tree"),
            _identity("root"),
            (
                SourceTreeEntry(
                    "README.md",
                    "generated-source",
                    SourceTreeEntryOrigin.GENERATED_TEXT,
                    blob,
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            occupied = parent / "occupied"
            occupied.mkdir()
            with self.assertRaisesRegex(ArtifactMaterializationError, "must be absent"):
                materialize_artifact_plan(
                    plan, occupied, read_blob=lambda _blob: content
                )

            failed = parent / "failed"
            with self.assertRaisesRegex(ArtifactMaterializationError, "does not match"):
                materialize_artifact_plan(
                    plan, failed, read_blob=lambda _blob: b"tampered"
                )
            self.assertFalse(failed.exists())


if __name__ == "__main__":
    unittest.main()
