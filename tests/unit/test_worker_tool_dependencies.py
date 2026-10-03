"""Worker native graphs retain real library evidence and exact tool selection."""

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.action_build_worker import ConfiguredBuildWorker
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_toolchains import WorkerToolchainRegistry
from literate_ai.adapters.dependencies import HostDependencyObservation
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.adapters.worker_tool_dependencies import (
    MAX_DEPENDENCY_BYTES,
    WorkerToolDependencies,
    capture_worker_tool_dependencies,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes

ROOT = "urn:literate-ai:component:worker-test"


class WorkerToolDependencyTests(unittest.TestCase):
    def setUp(self):
        self.binding = LocalComponentToolBinding(sys.executable)
        self.registry = WorkerToolchainRegistry((self.binding,))
        self.guard = Mock()

    def capture(self, **kwargs):
        return capture_worker_tool_dependencies(
            self.registry,
            self.registry.identities,
            environment=dict(os.environ),
            root_ref=ROOT,
            require_current=self.guard,
            **kwargs,
        )

    def test_real_native_interpreter_graph_round_trips_without_mutable_authority(self):
        value = self.capture()
        self.assertEqual(WorkerToolDependencies(value.document), value)
        self.assertTrue(value.observation.components)
        self.assertTrue(value.observation.edges)
        self.assertTrue(
            any(
                item.get("hashes") or item.get("version")
                for item in value.observation.components
            )
        )
        first = value.identity
        value.observation.components[0]["version"] = "tampered"
        self.assertEqual(value.identity, first)
        self.assertNotEqual(value.observation.components[0].get("version"), "tampered")
        self.guard.assert_called()

    def test_library_drift_changes_bound_graph_and_exact_commands_are_observed(self):
        with patch(
            "literate_ai.adapters.worker_tool_dependencies.PortableHostDependencyObserver"
        ) as observer:

            def graph(version):
                return HostDependencyObservation(
                    ({"bom-ref": "library", "type": "library", "version": version},),
                    ((ROOT, "library"),),
                )

            observer.return_value.observe.return_value = graph("one")
            first = self.capture()
            self.assertEqual(
                observer.call_args.kwargs["toolchain_commands"], (self.binding.command,)
            )
            observer.return_value.observe.return_value = graph("two")
            self.assertNotEqual(first.identity, self.capture().identity)
            self.guard.side_effect = RuntimeError("deadline expired")
            with self.assertRaisesRegex(RuntimeError, "deadline expired"):
                self.capture()

    def test_oversized_observation_reports_sizes_without_private_graph_values(self):
        private = "private-worker-path"
        with patch(
            "literate_ai.adapters.worker_tool_dependencies.PortableHostDependencyObserver"
        ) as observer:
            observer.return_value.observe.return_value = HostDependencyObservation(
                ({"bom-ref": private, "payload": "x" * MAX_DEPENDENCY_BYTES},),
                ((ROOT, private),),
            )
            with self.assertRaises(ValueError) as error:
                self.capture()
        message = str(error.exception)
        self.assertIn("components=1, edges=1", message)
        self.assertRegex(message, r"component_bytes=[0-9]+, edge_bytes=[0-9]+")
        self.assertNotIn(private, message)
        self.assertLess(len(message), 256)

    def test_unknown_selection_refuses_before_native_observation(self):
        with patch(
            "literate_ai.adapters.worker_tool_dependencies.PortableHostDependencyObserver"
        ) as observer:
            with self.assertRaises(ActionWireError):
                capture_worker_tool_dependencies(
                    self.registry,
                    (canonical_identity("missing"),),
                    environment={},
                    root_ref=ROOT,
                    require_current=self.guard,
                )
            observer.assert_not_called()

    def test_configured_worker_reobserves_and_refuses_changed_library(self):
        worker = ConfiguredBuildWorker(self.binding, (self.binding,), environment={})
        with patch(
            "literate_ai.adapters.worker_tool_dependencies.PortableHostDependencyObserver"
        ) as observer:
            observer.return_value.observe.return_value = HostDependencyObservation(
                ({"bom-ref": "library", "version": "one"},),
                ((ROOT, "library"),),
            )
            args = dict(root_ref=ROOT, require_current=self.guard)
            first = worker.observe_tool_dependencies(self.registry.identities, **args)
            worker.observe_tool_dependencies(
                self.registry.identities, expected_identity=first.identity, **args
            )
            observer.return_value.observe.return_value = HostDependencyObservation(
                ({"bom-ref": "library", "version": "two"},),
                ((ROOT, "library"),),
            )
            with self.assertRaises(ActionWireError) as caught:
                worker.observe_tool_dependencies(
                    self.registry.identities, expected_identity=first.identity, **args
                )
            self.assertEqual(caught.exception.code, "action_tools.dependencies_changed")

    def test_closed_bounded_graph_rejects_dangling_unreachable_and_duplicate_entries(
        self,
    ):
        document = dict(
            schema="literate-ai/worker-tool-dependencies@1",
            tools=[self.binding.toolchain_identity.uri],
            root=ROOT,
            components=[{"bom-ref": "library", "type": "library"}],
            edges=[[ROOT, "library"]],
        )
        good = canonical_json_bytes(document)
        WorkerToolDependencies(good)
        invalid = (
            good + b" ",
            b"x" * (MAX_DEPENDENCY_BYTES + 1),
            good.replace(b'"schema":', b'"root":"duplicate","schema":'),
            canonical_json_bytes(document | {"extra": True}),
            canonical_json_bytes(document | {"edges": [[ROOT, "missing"]]}),
            canonical_json_bytes(document | {"edges": []}),
            canonical_json_bytes(document | {"components": document["components"] * 2}),
            canonical_json_bytes(document | {"tools": ["untyped"]}),
        )
        for content in invalid:
            with self.subTest(size=len(content)), self.assertRaises(ValueError):
                WorkerToolDependencies(content)

    def test_environment_override_and_separate_tool_contexts_are_preserved(self):
        second = LocalComponentToolBinding(
            sys.executable, environment=(("PATH", "/private/tool"),)
        )
        registry = WorkerToolchainRegistry((self.binding, second))
        with (
            patch(
                "literate_ai.adapters.worker_tool_dependencies.sys",
                SimpleNamespace(platform="win32"),
            ),
            patch(
                "literate_ai.adapters.worker_tool_dependencies.PortableHostDependencyObserver"
            ) as observer,
        ):
            observer.return_value.observe.return_value = HostDependencyObservation(
                ({"bom-ref": "same-image", "type": "library"},),
                ((ROOT, "same-image"),),
            )
            value = capture_worker_tool_dependencies(
                registry,
                registry.identities,
                environment={"Path": "/base"},
                root_ref=ROOT,
                require_current=self.guard,
            )
        self.assertEqual(len(value.observation.components), 2)
        self.assertEqual(len(json.loads(value.document)["tools"]), 2)
        environments = [
            call.kwargs["windows_environment"] for call in observer.call_args_list
        ]
        self.assertIn({"PATH": "/private/tool"}, environments)
        self.assertIn({"Path": "/base"}, environments)

    def test_each_selected_tool_requires_a_complete_graph(self):
        second = LocalComponentToolBinding(
            sys.executable, environment=(("PRIVATE", "2"),)
        )
        registry = WorkerToolchainRegistry((self.binding, second))
        with patch(
            "literate_ai.adapters.worker_tool_dependencies.PortableHostDependencyObserver"
        ) as observer:
            observer.return_value.observe.side_effect = [
                HostDependencyObservation(
                    ({"bom-ref": "library"},), ((ROOT, "library"),)
                ),
                HostDependencyObservation((), ()),
            ]
            with self.assertRaisesRegex(ValueError, "empty or invalid"):
                capture_worker_tool_dependencies(
                    registry,
                    registry.identities,
                    environment={},
                    root_ref=ROOT,
                    require_current=self.guard,
                )

    def test_indexed_edges_preserve_dense_graph_beyond_legacy_wire_bound(self):
        from literate_ai.adapters.worker_tool_dependencies import _graph_document

        components = [
            {"bom-ref": f"urn:dependency:{i:04d}:" + "r" * 90, "evidence": "e" * 2200}
            for i in range(1139)
        ]
        refs = [item["bom-ref"] for item in components]
        edges = sorted(
            {(ROOT, ref) for ref in refs}
            | {
                (ref, refs[(i + offset) % len(refs)])
                for i, ref in enumerate(refs)
                for offset in range(1, 15)
            }
        )
        legacy = canonical_json_bytes(
            dict(
                schema="literate-ai/worker-tool-dependencies@1",
                root=ROOT,
                tools=[self.binding.toolchain_identity.uri],
                components=components,
                edges=edges,
            )
        )
        self.assertGreater(len(legacy), MAX_DEPENDENCY_BYTES)
        content = _graph_document(
            tools=[self.binding.toolchain_identity.uri],
            root=ROOT,
            components=components,
            edges=edges,
        )
        self.assertLess(len(content), MAX_DEPENDENCY_BYTES)
        graph = WorkerToolDependencies(content)
        self.assertEqual(graph.observation.components, tuple(components))
        self.assertEqual(graph.observation.edges, tuple(edges))

    def test_indexed_edges_reject_noncanonical_invalid_and_amplified_graphs(self):
        document = dict(
            schema="literate-ai/worker-tool-dependencies@2",
            tools=[self.binding.toolchain_identity.uri],
            root=ROOT,
            components=[{"bom-ref": "a"}, {"bom-ref": "b"}],
            edges=[[0, 1], [1, 2]],
        )
        WorkerToolDependencies(canonical_json_bytes(document))
        for edges in (
            [[False, 1]],
            [[-1, 1]],
            [[0, 3]],
            [[0, "1"]],
            [[1, 2], [0, 1]],
            [[0, 1], [0, 1]],
            [[0, 1]],
            [[1, 1]],
            [[0, 1, 2]],
        ):
            with self.subTest(edges=edges), self.assertRaises(ValueError):
                WorkerToolDependencies(
                    canonical_json_bytes(document | {"edges": edges})
                )
        components = [{"bom-ref": f"{i:03d}:" + "x" * 2048} for i in range(100)]
        edges = [[0, i] for i in range(1, 101)] + [
            [a, b] for a in range(1, 101) for b in range(1, 101) if a != b
        ]
        content = canonical_json_bytes(
            document | {"components": components, "edges": edges}
        )
        self.assertLess(len(content), MAX_DEPENDENCY_BYTES)
        with self.assertRaisesRegex(ValueError, "expanded dependency"):
            WorkerToolDependencies(content)
