"""Native guard scopes reuse parsing, never source observations or permissions."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import literate_ai.project_authority_graph as graph
from literate_ai.adapters import legacy_generation_catalog as catalog
from literate_ai.adapters.retained_provider_native import RetainedProviderNative
from literate_ai.application.generation_preparation import GenerationPreparationError
from literate_ai.contracts.identity import canonical_identity
from tests.critical import test_current_component_locks as fixtures


class RetainedNativeObservationScopeTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.CurrentComponentLockTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.root
        self.flavor = self.root / "flavors/os-macos/flavor.md"
        self.expected = graph.project_authority_graph(
            self.root, include_component_locks=False
        )
        self.graphs = []
        self.provider_calls = 0
        self.toolchain_calls = 0
        self.inputs = Mock()
        self.toolchain_action = lambda: None

        def provider_guard():
            self.provider_calls += 1
            actual = graph.project_authority_graph(
                self.root, include_component_locks=False
            )
            self.graphs.append(actual)
            if actual != self.expected:
                raise ValueError("changed real project graph")

        def toolchain_guard():
            self.toolchain_calls += 1
            self.toolchain_action()

        oracle = SimpleNamespace(identity=canonical_identity({"oracle": "fixture"}))
        # These counted callbacks test the public guard's orchestration only.
        # Graph/file reads are real; this is not a provider/admission fixture.
        self.native = RetainedProviderNative(
            SimpleNamespace(require_unchanged=provider_guard),
            SimpleNamespace(require_unchanged=toolchain_guard),
            oracle,
            oracle.identity,
            self.inputs,
        )

    def test_two_calls_keep_every_guard_and_graph_but_parse_once_per_operation(self):
        with patch.object(
            catalog,
            "_load_legacy_flavor_entries",
            wraps=catalog._load_legacy_flavor_entries,
        ) as load:
            self.native.require_unchanged()
            self.native.require_unchanged()
        self.assertEqual(load.call_count, 2)
        self.assertEqual(len(self.graphs), 4)
        self.assertIsNot(self.graphs[0], self.graphs[1])
        self.assertEqual(self.provider_calls, 4)
        self.assertEqual(self.toolchain_calls, 4)
        self.assertEqual(self.inputs.require_unchanged.call_count, 4)

    def test_next_operation_reopens_changed_bytes(self):
        self.native.require_unchanged()
        self.flavor.write_bytes(self.flavor.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            self.native.require_unchanged()

    def test_late_flavor_mutation_after_last_graph_refuses(self):
        def change():
            if self.toolchain_calls == 2:
                self.flavor.write_bytes(self.flavor.read_bytes() + b"\n")

        self.toolchain_action = change
        with self.assertRaises(ValueError):
            self.native.require_unchanged()

    def test_late_project_discovery_mutation_after_last_graph_refuses(self):
        def change():
            if self.toolchain_calls == 2:
                path = self.root / "literate.project.json"
                path.write_bytes(path.read_bytes() + b"\n")

        self.toolchain_action = change
        with self.assertRaises(GenerationPreparationError):
            self.native.require_unchanged()

    def test_exception_does_not_leave_an_observation_for_the_next_call(self):
        def fail():
            raise ValueError("injected tool observation refusal")

        self.toolchain_action = fail
        with self.assertRaisesRegex(ValueError, "injected tool observation refusal"):
            self.native.require_unchanged()
        self.toolchain_action = lambda: None
        with patch.object(
            catalog,
            "_load_legacy_flavor_entries",
            wraps=catalog._load_legacy_flavor_entries,
        ) as load:
            self.native.require_unchanged()
        self.assertEqual(load.call_count, 1)


if __name__ == "__main__":
    unittest.main()
