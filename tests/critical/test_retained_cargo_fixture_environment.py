"""Private HOME must not silently change the actual Rustup toolchain root."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import fixtures_retained_cargo_native_v2 as fixture


class NativeReceivingEnvironmentTests(unittest.TestCase):
    def test_absent_root_is_captured_from_parent_home_before_isolation(self):
        with patch.dict(os.environ, {"HOME": "/original-home"}, clear=True):
            before = dict(os.environ)
            with patch.object(Path, "home", return_value=Path("/original-home")):
                captured = fixture._rustup_home_before_isolation()
            self.assertEqual(dict(os.environ), before)
            with patch.dict(os.environ, {"HOME": "/private-fixture-home"}):
                self.assertEqual(captured, str(Path("/original-home") / ".rustup"))

    def test_explicit_root_does_not_consult_default_home(self):
        with patch.dict(os.environ, {"RUSTUP_HOME": "explicit-root"}, clear=True):
            with patch.object(Path, "home", side_effect=AssertionError("no fallback")):
                self.assertEqual(
                    fixture._rustup_home_before_isolation(), "explicit-root"
                )

    def test_explicit_empty_root_is_preserved_for_discovery_to_validate(self):
        with patch.dict(os.environ, {"RUSTUP_HOME": ""}, clear=True):
            with patch.object(Path, "home", side_effect=AssertionError("no fallback")):
                self.assertEqual(fixture._rustup_home_before_isolation(), "")

    def test_explicit_invalid_root_is_preserved_for_discovery_to_refuse(self):
        with patch.dict(os.environ, {"RUSTUP_HOME": "absent/toolchain"}, clear=True):
            with patch.object(Path, "home", side_effect=AssertionError("no fallback")):
                self.assertEqual(
                    fixture._rustup_home_before_isolation(), "absent/toolchain"
                )
