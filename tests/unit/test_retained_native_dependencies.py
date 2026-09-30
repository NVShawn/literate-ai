"""Native graph changes refuse even when the captured file bytes still match."""

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.dependencies.observation import PortableHostDependencyObserver
from literate_ai.adapters.dependencies.types import HostDependencyObservation
from literate_ai.adapters.retained_native_dependencies import (
    observe_retained_native_dependencies,
)


class RetainedNativeDependencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ng-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.binary = self.root / "binary"
        self.binary.write_bytes(b"observed native")
        self.graph = HostDependencyObservation(
            (
                {
                    "bom-ref": "binary",
                    "hashes": [
                        {
                            "alg": "SHA-256",
                            "content": hashlib.sha256(
                                self.binary.read_bytes()
                            ).hexdigest(),
                        }
                    ],
                    "properties": [
                        {"name": "literate-ai:elf-path", "value": str(self.binary)}
                    ],
                },
                {
                    "bom-ref": "system-image",
                    "properties": [
                        {"name": "literate-ai:macho-uuid:arm64", "value": "original"}
                    ],
                },
            ),
            (("binary", "system-image"),),
        )

    def observe(self, **kwargs):
        return observe_retained_native_dependencies(
            artifact_root=self.root,
            artifact_files=(self.binary,),
            root_ref="root",
            **kwargs,
        )

    def test_initial_capture_reobserves_and_later_checks_use_frozen_selection(self):
        roots = [self.root]
        environment = {"SystemRoot": str(self.root)}
        calls = []

        def observe(observer, build, *, root_ref):
            calls.append(
                (
                    observer.artifact_files,
                    observer.library_roots,
                    observer.windows_environment,
                    build,
                    root_ref,
                )
            )
            return self.graph

        with patch.object(PortableHostDependencyObserver, "observe", observe):
            captured = self.observe(
                library_roots=roots, windows_environment=environment
            )
            self.assertEqual(len(calls), 2)
            roots.clear()
            environment.clear()
            captured.require_unchanged()
            self.assertEqual(calls[0], calls[2])

    def test_macos_loader_environment_is_frozen_for_reobservation(self):
        environment = {
            "DYLD_LIBRARY_PATH": "/original",
            "DYLD_FRAMEWORK_PATH": "/frameworks",
        }
        expected = dict(environment)
        calls = []

        def observe(observer, build, *, root_ref):
            calls.append(observer.macos_loader_paths.to_environment())
            return self.graph

        with patch.object(PortableHostDependencyObserver, "observe", observe):
            captured = self.observe(macos_environment=environment)
            environment["DYLD_LIBRARY_PATH"] = "/changed"
            captured.require_unchanged()
        self.assertEqual(calls, [expected, expected, expected])

    def test_linux_loader_environment_is_frozen_for_reobservation(self):
        environment = {"LD_LIBRARY_PATH": "/original;/second"}
        calls = []

        def observe(observer, build, *, root_ref):
            calls.append(observer.linux_loader_paths.to_environment())
            return self.graph

        with patch.object(PortableHostDependencyObserver, "observe", observe):
            captured = self.observe(linux_environment=environment)
            environment["LD_LIBRARY_PATH"] = "/changed"
            captured.require_unchanged()
        self.assertEqual(calls, [{"LD_LIBRARY_PATH": "/original:/second"}] * 3)

    def test_changed_system_image_or_edges_refuse_with_unchanged_files(self):
        for kind in ("uuid", "edge"):
            changed = copy.deepcopy(self.graph)
            if kind == "uuid":
                changed.components[1]["properties"][0]["value"] = "changed"
            else:
                changed = HostDependencyObservation(changed.components, ())
            with (
                self.subTest(kind=kind),
                patch.object(
                    PortableHostDependencyObserver,
                    "observe",
                    side_effect=[self.graph, self.graph, changed],
                ),
            ):
                captured = self.observe()
                with self.assertRaisesRegex(ValueError, "observation-changed"):
                    captured.require_unchanged()

    def test_graph_drift_during_initial_capture_refuses(self):
        changed = HostDependencyObservation(self.graph.components, ())
        with patch.object(
            PortableHostDependencyObserver, "observe", side_effect=[self.graph, changed]
        ):
            with self.assertRaisesRegex(ValueError, "observation-changed"):
                self.observe()

    def test_file_drift_prevents_further_inspection(self):
        with patch.object(
            PortableHostDependencyObserver, "observe", return_value=self.graph
        ) as observe:
            captured = self.observe()
            self.binary.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                captured.require_unchanged()
            self.assertEqual(observe.call_count, 2)
