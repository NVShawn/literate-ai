"""Native hardlink paths preserve immutable CAS publication at long destinations."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from literate_ai.storage import BlobIntegrityError, FileSystemCAS, StorageSafetyError
from literate_ai.storage.cas import _native_cas_path


class CasNativePathTests(unittest.TestCase):
    def test_windows_drive_unc_and_extended_paths_are_absolute_and_preserved(self):
        cases = (
            (r"C:\cache\blob", r"\\?\C:\cache\blob"),
            (r"\\server\share\cache\blob", r"\\?\UNC\server\share\cache\blob"),
            (r"\\?\C:\cache\blob", r"\\?\C:\cache\blob"),
            (r"\\?\UNC\server\share\cache\blob", r"\\?\UNC\server\share\cache\blob"),
        )
        for source, expected in cases:
            path = PureWindowsPath(source)
            with (
                self.subTest(source=source),
                patch("literate_ai.storage.cas.os.name", "nt"),
            ):
                self.assertEqual(_native_cas_path(path), expected)
        relative = PureWindowsPath("relative/blob")
        with (
            patch("literate_ai.storage.cas.os.name", "nt"),
            self.assertRaises(StorageSafetyError),
        ):
            _native_cas_path(relative)

    def test_non_windows_path_is_not_rewritten(self):
        path = Path("relative-cas/blob")
        with patch("literate_ai.storage.cas.os.name", "posix"):
            self.assertIs(_native_cas_path(path), path)

    def test_long_destination_concurrent_publication_and_corruption_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                cas = FileSystemCAS(root / ("c" * max(1, 215 - len(str(root)))))
                content = b"immutable native path evidence"
                with ThreadPoolExecutor(max_workers=6) as workers:
                    references = tuple(
                        workers.map(lambda _: cas.put_bytes(content), range(12))
                    )
                self.assertEqual(len(set(references)), 1)
                reference = references[0]
                destination = cas.path_for(reference)
                self.assertGreater(len(str(destination)), 260)
                self.assertEqual(cas.get_bytes(reference), content)
                self.assertTrue(cas.contains(reference))
                self.assertTrue(cas.contains(reference, verify=False))
                self.assertEqual(tuple(cas.iter_refs()), (reference,))
                copied = root / "copied.bin"
                cas.copy_to(reference, copied)
                self.assertEqual(copied.read_bytes(), content)
                other = FileSystemCAS(root / "short-cas")
                self.assertEqual(other.put_file(destination), reference)
                self.assertFalse(tuple(destination.parent.glob(".blob-*")))
                timestamp = destination.stat().st_mtime_ns
                self.assertEqual(cas.put_bytes(content), reference)
                self.assertEqual(destination.stat().st_mtime_ns, timestamp)
                destination.write_bytes(b"corrupt")
                with self.assertRaises(BlobIntegrityError):
                    cas.put_bytes(content)
                self.assertEqual(destination.read_bytes(), b"corrupt")
                self.assertFalse(tuple(destination.parent.glob(".blob-*")))
            finally:
                shutil.rmtree(Path(_native_cas_path(root)))
