"""Snapshot replay selects actual native authority, including versioned FreeBSD."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.self_hosting import SelfHostError, _host_os, _load_platform_flavor
from tests.conformance.support.self_hosting_proof import (
    SelfHostSampleError,
    platform_flavor,
)


class SnapshotPlatformTests(unittest.TestCase):
    def test_native_flavor_selection_and_unknown_host_refusal(self):
        root = Path(__file__).resolve().parents[2]
        for platform, flavor in (
            ("freebsd16", "freebsd"),
            ("freebsd14", "freebsd"),
            ("linux", "linux"),
            ("darwin", "macos"),
            ("win32", "windows"),
            ("cygwin", "windows"),
        ):
            with (
                self.subTest(platform=platform),
                patch.object(sys, "platform", platform),
            ):
                selected = platform_flavor(root)
                self.assertEqual(
                    selected, root / "flavors" / f"os-{flavor}" / "flavor.md"
                )
                self.assertTrue(selected.is_file())
                self.assertEqual(_host_os(), flavor)
        with patch.object(sys, "platform", "unqualified-os"):
            with self.assertRaisesRegex(SelfHostSampleError, "unsupported.*platform"):
                platform_flavor(root)
            with self.assertRaisesRegex(SelfHostError, "unsupported host platform"):
                _host_os()

    def test_core_admits_real_freebsd_flavor_and_refuses_foreign_flavor(self):
        root = Path(__file__).resolve().parents[2]
        with patch.object(sys, "platform", "freebsd16"):
            native = _load_platform_flavor(root / "flavors/os-freebsd/flavor.md")
            self.assertEqual(native.axis_value, "freebsd")
            with self.assertRaises(SelfHostError) as caught:
                _load_platform_flavor(root / "flavors/os-linux/flavor.md")
            self.assertEqual(
                caught.exception.code, "self-host.platform-flavor-mismatch"
            )
