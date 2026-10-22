"""Native code identity and complete edges survive reviewed external relocation."""

import unittest
from pathlib import Path

from literate_ai.adapters.retained_sdk_runtime_context import (
    _require_native_equivalence,
)


class NativeRuntimeEquivalenceTests(unittest.TestCase):
    def graphs(self):
        expected = {
            "/sdk/lib/api.so": "api-code",
            "/host/lib/runtime.so": "runtime-code",
            "/host/lib/base.so": "base-code",
        }
        edges = {
            "/sdk/lib/api.so": {"/host/lib/runtime.so"},
            "/host/lib/runtime.so": {"/host/lib/base.so"},
            "/host/lib/base.so": set(),
        }
        actual = {
            "/sdk/lib/api.so": "api-code",
            "/captured/lib/runtime.so": "runtime-code",
            "/host/lib/base.so": "base-code",
        }
        children = {
            "/sdk/lib/api.so": {"/captured/lib/runtime.so"},
            "/captured/lib/runtime.so": {"/host/lib/base.so"},
            "/host/lib/base.so": set(),
        }

        def paths(graph):
            return {str(Path(k)): v for k, v in graph.items()}

        def links(graph):
            return {
                str(Path(k)): {str(Path(v)) for v in children}
                for k, children in graph.items()
            }

        return (
            paths(actual),
            links(children),
            paths(expected),
            links(edges),
            {str(Path("/sdk/lib/api.so"))},
            (str(Path("/captured/lib")),),
            {str(Path("/captured/lib/runtime.so"))},
        )

    def test_reviewed_external_copy_preserves_complete_code_graph(self):
        _require_native_equivalence(*self.graphs())

    def test_external_copy_with_different_code_is_not_equivalent(self):
        values = self.graphs()
        values[0][str(Path("/captured/lib/runtime.so"))] = "foreign-code"
        with self.assertRaisesRegex(ValueError, "shadows-sdk-dependency"):
            _require_native_equivalence(*values)

    def test_identical_external_image_with_changed_edges_is_not_equivalent(self):
        values = self.graphs()
        values[1][str(Path("/captured/lib/runtime.so"))] = set()
        with self.assertRaisesRegex(ValueError, "shadows-sdk-dependency"):
            _require_native_equivalence(*values)
