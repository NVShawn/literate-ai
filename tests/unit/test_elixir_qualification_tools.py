"""Qualification staging must select the authored Bazel release explicitly."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.stage_elixir_qualification_tools import pinned_bazel_environment


class ElixirQualificationToolTests(unittest.TestCase):
    def test_repository_pin_overrides_latest_without_changing_ambient_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            (repository / ".bazelversion").write_text("9.2.0\n")
            with mock.patch.dict(
                "os.environ",
                {"USE_BAZEL_VERSION": "latest", "BAZELISK_HOME": "/private/cache"},
                clear=True,
            ):
                environment = pinned_bazel_environment(repository)
                self.assertEqual(environment["USE_BAZEL_VERSION"], "9.2.0")
                self.assertEqual(environment["BAZELISK_HOME"], "/private/cache")
                import os

                self.assertEqual(os.environ["USE_BAZEL_VERSION"], "latest")

    def test_missing_or_ambiguous_pin_refuses_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            with self.assertRaises(FileNotFoundError):
                pinned_bazel_environment(repository)
            for pin in ("latest\n", "9.x\n", "9.2.0\n9.3.0\n", "9.2.0 ", ""):
                with self.subTest(pin=pin):
                    (repository / ".bazelversion").write_text(pin)
                    with self.assertRaises(ValueError):
                        pinned_bazel_environment(repository)
