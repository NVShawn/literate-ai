"""Streamed native-file custody rechecks content, not only stat metadata."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.retained_native_file_custody import native_file_digest

_MODULE = "literate_ai.adapters.retained_native_file_custody"


class RetainedNativeFileCustodyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nf-")
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name).resolve() / "native"
        directory.mkdir()
        self.file = directory / "library"
        self.file.write_bytes(b"observed native file")

    def _same_size_rewrite_during_first_read(self):
        original = os.read
        changed = False

        def mutate(descriptor, count):
            nonlocal changed
            chunk = original(descriptor, count)
            if chunk and not changed:
                changed = True
                self.file.write_bytes(b"X" * len(chunk))
            return chunk

        return mutate

    def test_linux_recheck_does_not_depend_on_same_size_metadata_change(self):
        with (
            patch("os.read", side_effect=self._same_size_rewrite_during_first_read()),
            patch(f"{_MODULE}.sys.platform", "linux"),
            patch(f"{_MODULE}.os.name", "posix"),
            patch(f"{_MODULE}._signature", return_value=("same",)),
            patch(f"{_MODULE}._stat_binding_identity", return_value=("same",)),
            self.assertRaisesRegex(ValueError, "custody-changed"),
        ):
            native_file_digest(self.file, 1024)

    def test_platform_without_recheck_relies_on_stat_metadata(self):
        """Control: the Linux refusal above comes from the content recheck."""
        with (
            patch("os.read", side_effect=self._same_size_rewrite_during_first_read()),
            patch(f"{_MODULE}.sys.platform", "darwin"),
            patch(f"{_MODULE}.os.name", "posix"),
            patch(f"{_MODULE}._signature", return_value=("same",)),
            patch(f"{_MODULE}._stat_binding_identity", return_value=("same",)),
        ):
            native_file_digest(self.file, 1024)


if __name__ == "__main__":
    unittest.main()
