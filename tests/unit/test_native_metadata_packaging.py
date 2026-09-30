"""Native metadata archives for apt/brew/winget/chocolatey packaging Flavors."""

from __future__ import annotations

import unittest

from literate_ai.adapters.packaging import native_metadata_resource
from literate_ai.cli.package import _provider_name


class NativeMetadataPackagingTests(unittest.TestCase):
    def test_provider_names_cover_native_flavors(self) -> None:
        self.assertEqual(
            _provider_name({"coordinate": "flavor://literate-ai/package-apt"}),
            "apt",
        )
        self.assertEqual(
            _provider_name({"coordinate": "flavor://literate-ai/package-brew"}),
            "brew",
        )
        self.assertEqual(
            _provider_name({"coordinate": "flavor://literate-ai/package-winget"}),
            "winget",
        )
        self.assertEqual(
            _provider_name({"coordinate": "flavor://literate-ai/package-chocolatey"}),
            "chocolatey",
        )

    def test_metadata_files_are_deterministic(self) -> None:
        path, content = native_metadata_resource("apt", "hello", "1.0.0")
        self.assertEqual(path, "DEBIAN/control")
        self.assertIn(b"Package: hello", content)
        brew_path, brew = native_metadata_resource("brew", "hello", "1.0.0")
        self.assertTrue(brew_path.endswith(".rb"))
        self.assertIn(b'version "1.0.0"', brew)
        winget_path, winget = native_metadata_resource("winget", "hello", "1.0.0")
        self.assertEqual(winget_path, "manifest.yaml")
        self.assertIn(b"PackageIdentifier: hello", winget)
        nupkg_path, nupkg = native_metadata_resource("chocolatey", "hello", "1.0.0")
        self.assertTrue(nupkg_path.endswith(".nuspec"))
        self.assertIn(b"<id>hello</id>", nupkg)
