"""Scoped local launchers do not weaken complete observed toolchain authority."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from literate_ai.adapters.lifecycle import (
    LocalComponentToolBinding,
    LocalSourceTreeRegistry,
    LocalStandardLifecycleError,
)
from literate_ai.adapters.lifecycle.standard_python import StandardPythonTarget
from literate_ai.adapters.standard_project import (
    LocalObservedToolchainAuthority,
    StandardCommandProjectionError,
    assemble_standard_lifecycle_ports,
    project_standard_toolchain_closure,
)
from literate_ai.contracts import (
    ComponentCommandPhase,
    StandardPythonWheelCommandProfile,
    canonical_identity,
)
from tests.support.fixtures_test_component_node_generation_preparation import _fixture
from tests.support.fixtures_test_standard_project_factory import (
    _command_contracts,
    _toolchain_closure,
)


class StandardScopedProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        _, self.execution = _fixture()
        self.contracts, self.bindings = _command_contracts(self.execution)
        self.full = _toolchain_closure(self.execution, self.contracts, self.bindings)
        self.guard = Mock()
        self.authorities = tuple(
            LocalObservedToolchainAuthority(identity, self.guard)
            for identity in self.full.record.toolchain_identities
        )

    def project(self, **changes):
        return project_standard_toolchain_closure(
            self.execution,
            **{
                "contracts": self.contracts,
                "tool_bindings": (),
                "command_phases": (),
                "toolchain_authorities": self.authorities,
                "dependency_observation": self.full.dependency_observation,
                "observer_identity": self.full.record.observer_identity,
                **changes,
            },
        )

    def assemble(self, closure, **changes):
        return assemble_standard_lifecycle_ports(
            source_trees=LocalSourceTreeRegistry(),
            object_root=self.root / "objects",
            toolchain_closure=closure,
            **{"command_phases": (), **changes},
        ).ports

    def test_empty_scope_keeps_portable_identity_and_live_observed_guards(self):
        with patch.object(
            LocalComponentToolBinding,
            "require_unchanged",
            side_effect=AssertionError("local launcher accessed"),
        ):
            closure = self.project()
            self.assertEqual(closure.record, self.full.record)
            before = self.guard.call_count
            ports = self.assemble(closure)
            self.assertGreater(self.guard.call_count, before)
            self.assertEqual(ports.tool_bindings, {})
            self.assertFalse(ports.locked_command_authority_is_current())
            for operation in (ports.build, ports.test, ports.execute):
                with self.assertRaisesRegex(LocalStandardLifecycleError, "scope"):
                    operation(None, ())

    def test_empty_scope_cannot_infer_or_substitute_observed_authority(self):
        for authorities in (
            (),
            (LocalObservedToolchainAuthority(canonical_identity("other"), self.guard),),
            self.authorities * 2,
        ):
            with (
                self.subTest(authorities=authorities),
                self.assertRaisesRegex(
                    ValueError, "observed toolchain authorities must cover"
                ),
            ):
                self.project(toolchain_authorities=authorities)
        with self.assertRaisesRegex(ValueError, "every and only execution toolchain"):
            self.project(tool_bindings=self.bindings)

    def test_observation_drift_refuses_before_creating_objects(self):
        closure = self.project()
        self.guard.side_effect = RuntimeError("worker tool changed")
        with self.assertRaisesRegex(ValueError, "observed toolchain changed"):
            self.assemble(closure)
        self.assertFalse((self.root / "objects").exists())

    def test_assembly_cannot_widen_scope_even_when_phases_share_a_tool(self):
        closure = self.project(
            command_phases=(ComponentCommandPhase.BUILD,), tool_bindings=self.bindings
        )
        self.assertEqual(closure.record, self.full.record)
        for phases in ((ComponentCommandPhase.TEST,), tuple(ComponentCommandPhase)):
            with (
                self.subTest(phases=phases),
                self.assertRaisesRegex(ValueError, "exceeds projected"),
            ):
                self.assemble(closure, command_phases=phases)
        self.assertFalse((self.root / "objects").exists())
        self.assemble(closure)
        self.assemble(closure, command_phases=(ComponentCommandPhase.BUILD,))

    def test_changed_binding_set_or_noncanonical_scope_is_refused(self):
        for closure in (
            replace(self.full, tool_bindings=()),
            replace(self.full, tool_bindings=self.bindings * 2),
            replace(self.full, command_phases=("build",)),
            replace(self.full, command_phases=(ComponentCommandPhase.BUILD,) * 2),
        ):
            with self.subTest(closure=closure), self.assertRaises(ValueError):
                self.assemble(closure)
        self.assertFalse((self.root / "objects").exists())

    def python_target(self):
        contract = self.contracts[0]
        profile = StandardPythonWheelCommandProfile(
            "pip", "python", "requirements.txt", "python-wheel-lock.json"
        )
        return StandardPythonTarget(
            contract.component_revision,
            canonical_identity("packaging"),
            profile.identity,
            contract.build_system_resolver_identity,
            contract.build_system_toolchain_identity,
            contract.language_runtime_identity,
            "requirements.txt",
            "python-wheel-lock.json",
            self.bindings[0].command,
        )

    def python_contracts(self, target):
        return tuple(
            replace(contract, locked_build_authority_identity=target.identity)
            if contract.component_revision == target.component_revision
            else contract
            for contract in self.contracts
        )

    def test_python_custody_needs_neither_interpreter_nor_wheelhouse(self):
        target = self.python_target()
        closure = self.project(
            contracts=self.python_contracts(target), python_targets=(target,)
        )
        with patch.object(
            LocalComponentToolBinding,
            "require_unchanged",
            side_effect=AssertionError("local interpreter accessed"),
        ):
            ports = self.assemble(closure)
        self.assertEqual(ports.python_targets[target.component_revision.uri], target)
        self.assertEqual(ports.tool_bindings, {})
        self.assertIsNone(ports.python_wheelhouse)

    def test_executing_python_still_requires_matching_interpreter_and_wheelhouse(self):
        target = self.python_target()
        closure = self.project(
            contracts=self.python_contracts(target),
            python_targets=(target,),
            command_phases=tuple(ComponentCommandPhase),
            tool_bindings=self.bindings,
        )
        for phases in ((ComponentCommandPhase.BUILD,), tuple(ComponentCommandPhase)):
            with (
                self.subTest(phases=phases),
                self.assertRaises(StandardCommandProjectionError),
            ):
                self.assemble(closure, command_phases=phases)
        changed = replace(target, python_command=("unobserved-python",))
        with self.assertRaisesRegex(ValueError, "Python target command differs"):
            self.project(
                contracts=self.python_contracts(changed),
                python_targets=(changed,),
                command_phases=tuple(ComponentCommandPhase),
                tool_bindings=self.bindings,
            )
