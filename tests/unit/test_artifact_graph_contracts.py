"""BUILD-220 adapter-neutral artifact graph and immutable assembly tests."""

from __future__ import annotations

import hashlib
import unittest

from literate_ai.application.artifact_graph import (
    ArtifactAssemblyError,
    assemble_source_tree,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.executable_components.artifacts import (
    GeneratedTextFile,
    GeneratedTextTree,
)
from literate_ai.contracts.executable_components.assets import AuthoredBinaryAsset
from literate_ai.contracts.identity import ContentIdentity, canonical_identity


def identity(label: str) -> ContentIdentity:
    return canonical_identity({"fixture": label})


def blob(content: bytes, media_type: str) -> BlobRef:
    return BlobRef(
        hashlib.sha256(content).hexdigest(), len(content), media_type=media_type
    )


class SourceAssemblyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.component = identity("component")
        self.target = identity("target")
        self.authorization = identity("authorization")
        self.text = b"def answer():\n    return 42\n"
        self.icon = b"\x89PNG\r\n\x1a\n"
        self.text_blob = blob(self.text, "text/x-python")
        self.icon_blob = blob(self.icon, "image/png")
        self.contents = {
            self.text_blob.identity: self.text,
            self.icon_blob.identity: self.icon,
        }

    def generated(self, path: str = "src/app.py") -> GeneratedTextTree:
        return GeneratedTextTree(
            self.component,
            self.target,
            self.authorization,
            (GeneratedTextFile(path, self.text_blob),),
        )

    def asset(self, path: str = "assets/icon.png") -> AuthoredBinaryAsset:
        return AuthoredBinaryAsset(
            component_revision=self.component,
            asset_id="icon",
            path=path,
            role="runtime-asset",
            target_identity=self.target,
            blob=self.icon_blob,
            authorization_identity=self.authorization,
        )

    def read(self, reference: BlobRef) -> bytes:
        return self.contents[reference.identity]

    def test_model_cannot_overwrite_content_locked_asset(self) -> None:
        with self.assertRaisesRegex(ArtifactAssemblyError, "cannot overwrite"):
            assemble_source_tree(
                self.generated("assets/icon.png"), (self.asset(),), read_blob=self.read
            )

    def test_rejects_wrong_blob_bytes_and_non_utf8_model_output(self) -> None:
        with self.assertRaisesRegex(ArtifactAssemblyError, "digest mismatch"):
            assemble_source_tree(
                self.generated(),
                (),
                read_blob=lambda _: b"same-length-wrong-bytes........."[
                    : len(self.text)
                ],
            )
        bad = b"\xff"
        bad_blob = blob(bad, "text/plain")
        generated = GeneratedTextTree(
            self.component,
            self.target,
            self.authorization,
            (GeneratedTextFile("bad.txt", bad_blob),),
        )
        with self.assertRaisesRegex(ArtifactAssemblyError, "not UTF-8"):
            assemble_source_tree(generated, (), read_blob=lambda _: bad)


if __name__ == "__main__":
    unittest.main()
