"""Artifact custody has identical ordering on native Windows and POSIX."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.lifecycle import local_tree_identity
from literate_ai.contracts import canonical_identity


class PortableArtifactTreeTests(unittest.TestCase):
    def test_case_sensitive_components_preserve_posix_custody_on_every_host(self):
        # Component order puts the directory before its dotted sibling. Comparing
        # whole slash-separated strings would change existing POSIX identities.
        ordered = ("Z.txt", "a/child.txt", "a.txt", "b.txt")
        document = {
            "schema": "literate-ai/local-source-tree@1",
            "files": [
                {
                    "path": name,
                    "sha256": hashlib.sha256(name.encode()).hexdigest(),
                }
                for name in ordered
            ],
        }
        expected = canonical_identity(document)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, names in enumerate((ordered, tuple(reversed(ordered)))):
                tree = root / str(index)
                tree.mkdir()
                for name in names:
                    path = tree / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(name.encode())
                self.assertEqual(local_tree_identity(tree), expected)
                (tree / "Z.txt").write_bytes(b"changed")
                self.assertNotEqual(local_tree_identity(tree), expected)


if __name__ == "__main__":
    unittest.main()
