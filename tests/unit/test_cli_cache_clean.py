"""Stable CLI envelopes for generated-source and object-cache cleanup."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.cli import main


def invoke(*arguments: str) -> tuple[int, str, str]:
    output = io.StringIO()
    errors = io.StringIO()
    status = main(arguments, stdout=output, stderr=errors)
    return status, output.getvalue(), errors.getvalue()


class CacheCleanCliTests(unittest.TestCase):
    def test_conflicting_cache_roots_return_stable_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            with patch.dict(
                os.environ,
                {"BUILD_DIR": "cache", "OBJ_DIR": "cache"},
                clear=False,
            ):
                status, output, errors = invoke("clean", "--project", str(project))

        self.assertEqual(status, 2)
        self.assertEqual(output, "")
        envelope = json.loads(errors)
        self.assertEqual(envelope["command"], "clean")
        self.assertEqual(envelope["error"]["code"], "cache.clean_refused")
        self.assertEqual(
            envelope["error"]["message"],
            "BUILD_DIR and OBJ_DIR must be different directories",
        )

    def test_protected_cache_root_returns_stable_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            with patch.dict(
                os.environ,
                {"BUILD_DIR": ".", "OBJ_DIR": "objects"},
                clear=False,
            ):
                status, output, errors = invoke(
                    "really-clean", "--project", str(project)
                )

        self.assertEqual(status, 2)
        self.assertEqual(output, "")
        envelope = json.loads(errors)
        self.assertEqual(envelope["command"], "really-clean")
        self.assertEqual(envelope["error"]["code"], "cache.clean_refused")
        self.assertEqual(
            envelope["error"]["message"],
            "BUILD_DIR resolves to a protected directory",
        )

    def test_unmarked_cache_root_returns_stable_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            object_root = project / "objects"
            object_root.mkdir()
            (object_root / "operator-data").write_text("keep", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"BUILD_DIR": "generated", "OBJ_DIR": "objects"},
                clear=False,
            ):
                status, output, errors = invoke("clean", "--project", str(project))

            self.assertEqual((object_root / "operator-data").read_text(), "keep")

        self.assertEqual(status, 2)
        self.assertEqual(output, "")
        envelope = json.loads(errors)
        self.assertEqual(envelope["command"], "clean")
        self.assertEqual(envelope["error"]["code"], "cache.clean_refused")
        self.assertIn(
            "refusing to remove unmarked cache directory", envelope["error"]["message"]
        )


if __name__ == "__main__":
    unittest.main()
