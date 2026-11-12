"""Remote observation custody remains live through the public native reader.

The lock, projection, oracle reads and file-drift checks are real. Provider
promotion history and remote tool observations are explicit synthetic fixtures;
these tests do not claim generation, remote execution or provider qualification.
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from literate_ai.adapters.component_acceptance import oracle_path
from literate_ai.adapters.retained_provider_authority import RetainedProviderAuthority
from literate_ai.adapters.retained_provider_native import read_retained_provider_native
from literate_ai.application.library_artifacts import project_library_import_surface
from literate_ai.contracts import canonical_identity
from literate_ai.projects import PinnedInputClosure
from tests.support.fixtures_test_standard_command_projection import (
    _locked_snapshot,
    _observation,
    _tool,
)


class RetainedProviderObserverCustodyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="rpc-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        _, cls.snapshot, execution = _locked_snapshot(cls.root, no_entrypoint=True)
        node = next(
            item
            for item in cls.snapshot.authority.lock.nodes
            if item.revision.identity == cls.snapshot.authority.lock.root_revision
        )
        surface = project_library_import_surface(
            node.revision.coordinate.name,
            "python",
            tuple(
                (i.capability, i.interface_identity) for i in node.interface_bindings
            ),
        )
        path = oracle_path(cls.root, node.revision.coordinate.name)
        path.parent.mkdir(parents=True)
        harness = b"# verifier fixture; never executed\n"
        path.with_name("harness.py").write_bytes(harness)
        path.write_text(
            json.dumps(
                {
                    "schema": "literate-ai/library-acceptance-oracle@1",
                    "component": node.revision.coordinate.name,
                    "specification_set_identity": (
                        node.revision.specification_set_identity.uri
                    ),
                    "public_interface_identities": sorted(
                        i.identity.uri for i in node.revision.public_interfaces
                    ),
                    "import_surface_identity": surface.identity.uri,
                    "language": "python",
                    "harness": "harness.py",
                    "harness_identity": "sha256:" + hashlib.sha256(harness).hexdigest(),
                    "cases": [
                        {
                            "case_id": "fixture-case",
                            "capability": surface.capabilities[0].capability,
                            "arguments": [2, 3],
                            "expected_result": 5,
                        }
                    ],
                }
            )
        )
        custody = PinnedInputClosure(
            maximum_files=1, maximum_file_bytes=4096, maximum_total_bytes=4096
        )
        marker = cls.root / "synthetic-provider-history"
        marker.write_bytes(b"explicit synthetic provider qualification history")
        custody.pin(marker, boundary=cls.root, label=marker.name)
        unchanged = SimpleNamespace(require_unchanged=custody.require_unchanged)
        cls.provider = RetainedProviderAuthority(
            SimpleNamespace(root=cls.root),
            SimpleNamespace(
                require_unchanged=cls.snapshot.require_unchanged,
                prepared=SimpleNamespace(locked_authority_snapshot=cls.snapshot),
                execution=execution,
            ),
            SimpleNamespace(
                require_unchanged=custody.require_unchanged,
                profile=SimpleNamespace(
                    cases=(
                        SimpleNamespace(
                            case_id="fixture-case", arguments=(2, 3), expected_result=5
                        ),
                    )
                ),
            ),
            unchanged,
            unchanged,
            custody,
        )

    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="rpo-")))
        self.dependency = root / "dependency-observation"
        self.dependency.write_bytes(b"current original-host dependency observation")
        self.custody = PinnedInputClosure(
            maximum_files=1, maximum_file_bytes=4096, maximum_total_bytes=4096
        )
        self.custody.pin(self.dependency, boundary=root, label=self.dependency.name)
        self.observations = []

        def observe(commands):
            self.observations.append(commands)
            self.custody.require_unchanged()
            return _observation(commands)

        self.arguments = dict(
            host_platform="macos",
            toolchain_discoverer=lambda name, *_: _tool(name),
            dependency_observer=observe,
            observer_identity=canonical_identity("synthetic original-host observer"),
            command_phases=(),
            dependency_guard=self.custody.require_unchanged,
        )

    def test_guarded_custody_projection_rechecks_real_dependency_drift(self):
        current = read_retained_provider_native(self.provider, **self.arguments)
        self.assertEqual(current.toolchain.command_phases, ())
        self.assertEqual(current.toolchain.tool_bindings, ())
        self.assertTrue(current.toolchain.toolchain_authorities)
        self.assertEqual(len(self.observations), 1)
        current.require_unchanged()
        self.dependency.write_bytes(b"changed original-host dependency observation")
        with self.assertRaises(ValueError):
            current.require_unchanged()
        self.assertEqual(
            self.dependency.read_bytes(),
            b"changed original-host dependency observation",
        )

    def test_missing_or_noncallable_guard_refuses_before_observation(self):
        arguments = dict(self.arguments)
        del arguments["dependency_guard"]
        for changes in ({}, {"dependency_guard": None}, {"dependency_guard": object()}):
            with self.subTest(guard=tuple(changes)):
                with self.assertRaisesRegex(
                    TypeError, "live-dependency-guard-required"
                ):
                    read_retained_provider_native(
                        self.provider, **{**arguments, **changes}
                    )
        self.assertEqual(self.observations, [])

    def test_local_command_projection_keeps_optional_dependency_guard(self):
        arguments = dict(self.arguments)
        del arguments["command_phases"]
        del arguments["dependency_guard"]
        current = read_retained_provider_native(self.provider, **arguments)
        self.assertTrue(current.toolchain.command_phases)
        self.assertTrue(current.toolchain.tool_bindings)
        self.assertIsNone(current.toolchain.dependency_guard)
        current.require_unchanged()
