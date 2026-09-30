"""Swift host-realization preflight tests."""

from __future__ import annotations

import unittest
from unittest import mock

from literate_ai.adapters.standard_project import (
    StandardCommandProjectionError,
    _discover_toolchain,
)
from literate_ai.contracts import ToolchainConstraint


class SwiftToolchainPreflightTests(unittest.TestCase):
    def test_apple_realization_probes_through_xcrun(self) -> None:
        sentinel = object()
        with mock.patch(
            "literate_ai.adapters.standard_project.discover_cpp_toolchain",
            return_value=sentinel,
        ) as discover:
            result = _discover_toolchain(
                "swift",
                ToolchainConstraint("swift", ("xcrun", "swiftc")),
                {"PATH": "/usr/bin"},
            )

        self.assertIs(result, sentinel)
        self.assertEqual(discover.call_args.args[0]["CXX"], "xcrun swiftc")

    def test_linux_failure_links_the_official_install_authority(self) -> None:
        with (
            mock.patch(
                "literate_ai.adapters.standard_project.discover_cpp_toolchain",
                side_effect=RuntimeError("missing"),
            ),
            mock.patch("literate_ai.adapters.standard_project.sys.platform", "linux"),
            self.assertRaises(StandardCommandProjectionError) as raised,
        ):
            _discover_toolchain(
                "swift",
                ToolchainConstraint("swift", ("swiftc",)),
                {"PATH": "/usr/bin"},
            )

        self.assertEqual(
            raised.exception.code,
            "standard_command.swift_toolchain_unavailable",
        )
        self.assertIn("https://www.swift.org/install/linux/", str(raised.exception))

    def test_windows_failure_links_the_official_install_authority(self) -> None:
        with (
            mock.patch(
                "literate_ai.adapters.standard_project.discover_cpp_toolchain",
                side_effect=RuntimeError("missing"),
            ),
            mock.patch("literate_ai.adapters.standard_project.sys.platform", "win32"),
            self.assertRaises(StandardCommandProjectionError) as raised,
        ):
            _discover_toolchain(
                "swift",
                ToolchainConstraint("swift", ("swiftc.exe",)),
                {"PATH": "C:\\Windows"},
            )

        self.assertIn("https://www.swift.org/install/windows/", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
