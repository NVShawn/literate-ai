"""Native root authority and real directory drift; caller environment is untrusted."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai._filesystem import UnsafeFilesystemPathError
from literate_ai.adapters import retained_windows_context as context


class RetainedWindowsContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / "system"
        (self.root / "System32").mkdir(parents=True)

    def capture(self):
        return context.RetainedWindowsSystemContext.capture()

    def test_only_observed_root_enters_environment(self):
        with patch.object(context, "_system_windows_directory", return_value=self.root):
            value = self.capture()
            with patch.dict(
                os.environ,
                {
                    "SystemRoot": "untrusted",
                    "WINDIR": "other",
                    "PATH": "caller",
                    "LITAI_FIXTURE_SECRET": "not-forwarded",
                },
            ):
                self.assertEqual(value.environment(), {"SystemRoot": str(self.root)})

    def test_native_root_drift_and_directory_replacement_refuse(self):
        with patch.object(context, "_system_windows_directory", return_value=self.root):
            value = self.capture()
            (self.root / "System32").rename(self.root / "old-system")
            (self.root / "System32").mkdir()
            with self.assertRaisesRegex(ValueError, "context-changed"):
                value.environment()
            value = self.capture()
        other = self.parent / "other"
        (other / "System32").mkdir(parents=True)
        with patch.object(context, "_system_windows_directory", return_value=other):
            with self.assertRaisesRegex(ValueError, "context-changed"):
                value.environment()

    def test_missing_system_directory_refuses_without_creation(self):
        (self.root / "System32").rmdir()
        with patch.object(context, "_system_windows_directory", return_value=self.root):
            with self.assertRaises(UnsafeFilesystemPathError):
                self.capture()
        self.assertFalse((self.root / "System32").exists())

    @unittest.skipIf(os.name == "nt", "POSIX symlink fixture")
    def test_redirected_system_directory_refuses(self):
        with patch.object(context, "_system_windows_directory", return_value=self.root):
            value = self.capture()
            (self.root / "System32").rename(self.root / "original")
            (self.root / "System32").symlink_to("original", target_is_directory=True)
            with self.assertRaises(UnsafeFilesystemPathError):
                value.require_current()

    @unittest.skipIf(os.name == "nt", "POSIX default environment")
    def test_posix_default_keeps_no_windows_context(self):
        with patch.dict(os.environ, {"SystemRoot": "untrusted"}):
            self.assertIsNone(context.retained_system_context())

    @unittest.skipUnless(os.name == "nt", "Requires actual Windows API")
    def test_actual_windows_api_ignores_caller_root_and_search_path(self):
        value = self.capture()
        with patch.dict(
            os.environ,
            {
                "SystemRoot": str(self.parent),
                "WINDIR": str(self.parent),
                "PATH": str(self.parent),
            },
        ):
            observed = context.RetainedWindowsSystemContext.capture()
            self.assertEqual(observed.root, value.root)
            self.assertEqual(observed.environment(), {"SystemRoot": str(value.root)})
