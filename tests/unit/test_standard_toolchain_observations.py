"""Worker tool observation records preserve measured data under closed bounds."""

import json
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.standard_toolchain_observations import (
    MAX_TOOL_OBSERVATION_BYTES,
    StandardToolObservation,
    StandardToolObservations,
    capture_standard_tool_observations,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes


def observation(role="python"):
    return StandardToolObservation(
        role,
        canonical_identity({"tool": role}),
        ("/worker/tools/" + role,),
        (("TOOL_PATH", "/worker/lib"),),
        "3.14.0",
        (3, 14, 0),
    )


class StandardToolObservationsTests(unittest.TestCase):
    def test_raw_compiler_versions_stay_unparsed_and_npm_keeps_node_binding(self):
        def tool(role, version):
            return SimpleNamespace(
                command=("/worker/" + role,),
                version=version,
                identity=canonical_identity(role).uri,
                require_unchanged=Mock(),
            )

        cpp, node, npm = (
            tool("cpp", "Compiler vendor build 2026"),
            tool("node", "22.0.0"),
            tool("npm", "11.0.0"),
        )
        npm.node = node
        value = capture_standard_tool_observations(
            "linux",
            {"cpp": cpp, "node": node, "npm": npm},
            require_current=lambda: None,
        )
        self.assertIsNone(value.tools[0].version_info)
        self.assertEqual(value.tools[1].version_info, (22, 0, 0))
        self.assertEqual(value.tools[2].node_identity.uri, node.identity)
        self.assertEqual(StandardToolObservations.from_bytes(value.to_bytes()), value)

    def test_real_guarded_python_capture_preserves_exact_runtime(self):
        tool = discover_python_toolchain(pinned_command=(sys.executable,))
        guard = Mock()
        value = capture_standard_tool_observations(
            "windows"
            if sys.platform == "win32"
            else "macos"
            if sys.platform == "darwin"
            else "linux",
            {"python": tool},
            require_current=guard,
        )
        decoded = StandardToolObservations.from_bytes(value.to_bytes())
        self.assertEqual(value, decoded)
        self.assertEqual(value.identity, decoded.identity)
        self.assertEqual(decoded.tools[0].command, tool.command)
        self.assertEqual(decoded.tools[0].toolchain_identity.uri, tool.identity)
        self.assertEqual(decoded.tools[0].version, tool.version)
        self.assertEqual(decoded.tools[0].version_info, tool.version_info[:3])
        self.assertEqual(guard.call_count, 3)

    def test_capture_rechecks_tool_and_transport_before_returning(self):
        for failure in (1, 2, 3):
            calls = [None] * (failure - 1) + [RuntimeError("tool drift")]
            tool = Mock(
                command=("/worker/python",),
                version="3.14.0",
                version_info=(3, 14, 0),
                environment=(),
                identity=canonical_identity("tool").uri,
            )
            tool.require_unchanged.side_effect = calls
            with (
                self.subTest(failure=failure),
                self.assertRaisesRegex(RuntimeError, "tool drift"),
            ):
                capture_standard_tool_observations(
                    "linux", {"python": tool}, require_current=lambda: None
                )
        tool.require_unchanged.side_effect = None
        guard = Mock(side_effect=[None, None, RuntimeError("expired")])
        with self.assertRaisesRegex(RuntimeError, "expired"):
            capture_standard_tool_observations(
                "linux", {"python": tool}, require_current=guard
            )

    def test_npm_requires_exact_observed_node(self):
        node = observation("node")
        npm = StandardToolObservation(
            "npm",
            canonical_identity("npm"),
            ("/worker/node", "/worker/npm.js"),
            (),
            "11.0.0",
            (11, 0, 0),
            node.toolchain_identity,
        )
        value = StandardToolObservations("linux", (node, npm))
        self.assertEqual(StandardToolObservations.from_bytes(value.to_bytes()), value)
        for tools in (
            (npm,),
            (node, replace(npm, node_identity=canonical_identity("other"))),
        ):
            with (
                self.subTest(tools=tools),
                self.assertRaisesRegex(ValueError, "selected Node"),
            ):
                StandardToolObservations("linux", tools)

    def test_ambiguous_and_unbounded_records_refuse(self):
        tool = observation()
        for changes in (
            {"role": "unknown"},
            {"command": ()},
            {"command": ("x",) * 65},
            {"command": ("x\0",)},
            {"version_info": (True, 0, 0)},
            {"version_info": (-1, 0, 0)},
            {"version_info": (3, 14)},
            {"environment": (("X", "a"), ("x", "b"))},
            {"environment": (("B", "a"), ("A", "b"))},
            {"environment": (("A=B", "x"),)},
            {"node_identity": canonical_identity("node")},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises((ValueError, TypeError)),
            ):
                replace(tool, **changes)
        for tools in (
            (tool, tool),
            (tool, replace(tool, role="cpp", command=("different",))),
        ):
            with self.subTest(tools=tools), self.assertRaises(ValueError):
                StandardToolObservations(
                    "linux", tuple(sorted(tools, key=lambda item: item.role))
                )
        with self.assertRaisesRegex(ValueError, "exceeds bound"):
            StandardToolObservations(
                "linux",
                (
                    replace(
                        tool,
                        environment=tuple(("K" + str(i), "x" * 8192) for i in range(9)),
                    ),
                ),
            )

    def test_wire_rejects_unknown_fields_duplicate_keys_and_noncanonical_bytes(self):
        value = StandardToolObservations("linux", (observation(),))
        original = json.loads(value.to_bytes())
        for content in (
            value.to_bytes() + b"\n",
            b"x" * (MAX_TOOL_OBSERVATION_BYTES + 1),
            b'{"schema":"x","schema":"x"}',
            canonical_json_bytes({**original, "extra": True}),
            canonical_json_bytes(
                {**original, "tools": [{**original["tools"][0], "extra": True}]}
            ),
            canonical_json_bytes(
                {**original, "tools": [{**original["tools"][0], "command": "python"}]}
            ),
            canonical_json_bytes(
                {**original, "tools": [{**original["tools"][0], "environment": ["AB"]}]}
            ),
            b"[]",
            b"\xff",
            b"[" * 10000,
        ):
            with self.subTest(content=content[:100]), self.assertRaises(ValueError):
                StandardToolObservations.from_bytes(content)
