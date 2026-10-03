"""Numeric compiler constraints consume recognized, identity-bound banners."""

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.builders.cpp import CppToolchain
from literate_ai.adapters.remote_standard_toolchains import RemoteStandardToolchains
from literate_ai.adapters.standard_toolchain_observations import (
    capture_standard_tool_observations,
)
from literate_ai.contracts import ToolchainConstraint
from tests.unit.test_remote_standard_toolchains import snapshot


class CompilerVersionObservationTests(unittest.TestCase):
    def tool(self, banner, family="gnu"):
        return CppToolchain(("/worker/compiler",), family, banner, "sha256:" + "a" * 64)

    def test_known_banners_preserve_raw_identity_and_numeric_version(self):
        for banner, family, expected in (
            ("Apple clang version 17.0.0 (clang-1700.0.13.3)", "gnu", (17, 0, 0)),
            ("clang version 20.1.0", "msvc", (20, 1, 0)),
            ("g++ (GCC) 14.2.0", "gnu", (14, 2, 0)),
            ("gcc (Ubuntu 13.3.0-6ubuntu2) 13.3.0", "gnu", (13, 3, 0)),
            ("Apple Swift version 6.0.3 (swiftlang-6.0.3.1)", "gnu", (6, 0, 3)),
            ("Swift version 6.1", "gnu", (6, 1, 0)),
            ("19.40.33811.0", "msvc", (19, 40, 33811)),
            ("Cuda compilation tools, release 12.9, V12.9.86", "gnu", (12, 9, 86)),
        ):
            with self.subTest(banner=banner):
                tool = self.tool(banner, family)
                identity = tool.identity
                self.assertEqual(tool.version_info, expected)
                self.assertEqual(tool.version, banner)
                self.assertEqual(tool.identity, identity)

    def test_unknown_ambiguous_and_out_of_range_banners_stay_unstructured(self):
        for banner in (
            "Vendor build 2026.10.1 based on clang 20.1.0",
            "clang version 20.1.0git",
            "Cuda compilation tools, release 12.9, V13.0.1",
            "19.40.33811",
            "clang version 999999999999.1.0",
        ):
            with self.subTest(banner=banner):
                self.assertIsNone(self.tool(banner).version_info)

    def test_captured_compiler_version_satisfies_remote_bounds_without_execution(self):
        compiler = self.tool("clang version 20.1.0")
        guard = Mock()
        observed = SimpleNamespace(
            command=compiler.command,
            identity=compiler.identity,
            version=compiler.version,
            version_info=compiler.version_info,
            environment=(),
            require_unchanged=guard,
        )
        inventory = capture_standard_tool_observations(
            "linux", {"cpp": observed}, require_current=guard
        )
        consumer = RemoteStandardToolchains(
            snapshot(*inventory.tools), require_current=guard
        )
        constraint = ToolchainConstraint(
            "cpp", minimum_version=(20,), maximum_exclusive_version=(21,)
        )
        self.assertEqual(
            consumer.discover("cpp", constraint, {}).version_info, (20, 1, 0)
        )
        with self.assertRaises(ActionWireError):
            consumer.discover(
                "cpp",
                replace(
                    constraint, minimum_version=(21,), maximum_exclusive_version=(22,)
                ),
                {},
            )

    def test_explicit_unknown_version_cannot_be_reinterpreted_by_capture(self):
        compiler = self.tool("19.40.33811")
        observed = SimpleNamespace(
            command=compiler.command,
            identity=compiler.identity,
            version=compiler.version,
            version_info=compiler.version_info,
            environment=(),
            require_unchanged=Mock(),
        )
        inventory = capture_standard_tool_observations(
            "linux", {"cpp": observed}, require_current=lambda: None
        )
        self.assertIsNone(inventory.tools[0].version_info)
        consumer = RemoteStandardToolchains(
            snapshot(*inventory.tools), require_current=lambda: None
        )
        with self.assertRaises(ActionWireError):
            consumer.discover(
                "cpp", ToolchainConstraint("cpp", minimum_version=(19,)), {}
            )
