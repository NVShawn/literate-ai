"""Regression coverage for `exclusive_cache_lock`'s swap detection on POSIX."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai._cache_lock import CacheLockError, exclusive_cache_lock


class ExclusiveCacheLockPosixTests(unittest.TestCase):
    """Confirms the POSIX guard still catches a hard-link swap directly."""

    @unittest.skipIf(os.name == "nt", "hard links exercised via POSIX semantics")
    def test_hard_link_replacement_is_detected_as_unsafe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory).resolve() / "lock"
            other_path = Path(directory) / "other"
            lock_path.write_bytes(b"")
            other_path.write_bytes(b"")

            original_open = os.open

            def swap_after_open(path, flags, mode=0o777, *args, **kwargs):
                descriptor = original_open(path, flags, mode, *args, **kwargs)
                if Path(path) == lock_path:
                    lock_path.unlink()
                    os.link(other_path, lock_path)
                return descriptor

            with patch.object(os, "open", side_effect=swap_after_open):
                with self.assertRaises(CacheLockError):
                    with exclusive_cache_lock(lock_path):
                        self.fail("must not publish once the lock path is swapped")


if __name__ == "__main__":
    unittest.main()
