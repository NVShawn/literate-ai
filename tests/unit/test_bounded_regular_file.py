"""Small retained files must not allocate their entire caller's byte budget."""

import os
import tempfile
import tracemalloc
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.cache.filesystem import SourceCacheError, _read_regular_file


class BoundedRegularFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "record"
        self.content = b"small retained record" * 10
        self.path.write_bytes(self.content)

    def read(self, maximum=64 * 1024 * 1024):
        return _read_regular_file(self.path, maximum_bytes=maximum, code="fixture.read")

    def test_small_file_has_small_allocation_under_large_budget(self):
        tracemalloc.start()
        try:
            self.assertEqual(self.read(), self.content)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_exact_limit_and_empty_file_read_without_relaxing_limit(self):
        self.assertEqual(self.read(len(self.content)), self.content)
        with self.assertRaises(SourceCacheError):
            self.read(len(self.content) - 1)
        self.path.write_bytes(b"")
        self.assertEqual(self.read(0), b"")

    def test_growth_and_truncation_after_fstat_refuse(self):
        fdopen = os.fdopen
        for changed in (b"larger" * len(self.content), b"short"):
            with self.subTest(size=len(changed)):
                self.path.write_bytes(self.content)

                @contextmanager
                def changed_stream(*args, content=changed, **kwargs):
                    self.path.write_bytes(content)
                    with fdopen(*args, **kwargs) as stream:
                        yield stream

                with patch(
                    "literate_ai.adapters.cache.filesystem.os.fdopen", changed_stream
                ):
                    with self.assertRaisesRegex(SourceCacheError, "changed size"):
                        self.read()
