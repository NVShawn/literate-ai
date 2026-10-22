"""Immutable native observations and command-context drift; no SDK admission."""

import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import retained_project_dependencies as dependencies
from literate_ai.adapters.dependencies import PortableHostDependencyObserver
from literate_ai.adapters.dependencies.types import (
    DependencyObservationError,
    HostDependencyObservation,
)
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.adapters.retained_project_inputs import (
    discover_retained_project_inputs,
)
from literate_ai.contracts import ContentIdentity
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.contracts.sbom import retained_project_bom_ref
from tests.critical import test_retained_project_execution as execution_fixtures
from tests.support.fixtures_retained_project_sbom import retained_project_sbom_fixture


class RetainedProjectDependencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "sdk").mkdir()
        tool = self.root / "sdk/tool"
        # The observer is explicitly simulated in this contract fixture.
        # Native inspection is exercised by the real compiled command group.
        tool.write_bytes(b"captured tool")
        tool.chmod(0o755)
        self.manifest = discover_retained_project_inputs(
            self.root,
            {"sdk": "sdk"},
            RetainedProjectLimits(10, 4096, 8192),
            executable_paths=("sdk/tool",),
        )
        plan, _locks = retained_project_sbom_fixture()
        identity = ContentIdentity.parse_uri(
            "sha256:" + self.manifest.members[0].sha256
        )
        self.plan = replace(
            plan,
            manifest_identity=self.manifest.identity,
            profile=replace(plan.profile, tools=(("tool", "sdk/tool", identity),)),
        )
        self.binding = LocalComponentToolBinding(str(tool), authority_identity=identity)
        self.environment = {
            "PATH": str(tool.parent),
            "HOME": str(self.root),
            "LC_ALL": "C",
        }
        self.root_ref = retained_project_bom_ref(self.plan.identity)
        self.raw = HostDependencyObservation(
            (
                {
                    "bom-ref": "urn:fixture:library",
                    "type": "file",
                    "name": "library",
                    "version": "1",
                },
            ),
            ((self.root_ref, "urn:fixture:library"),),
        )
        self.factory = self.enterContext(
            patch.object(dependencies, "PortableHostDependencyObserver")
        )
        self.factory.return_value.observe.return_value = self.raw

    def guard(self, **changes):
        values = dict(
            inputs=self.root,
            manifest=self.manifest,
            plan=self.plan,
            tools=(self.binding,),
            environment=self.environment,
        )
        return dependencies.RetainedProjectDependencyGuard(**{**values, **changes})

    def test_exported_dict_mutation_cannot_change_frozen_custody(self):
        guard = self.guard()
        identity = guard.identity
        guard.observation.components[0]["version"] = "forged"
        self.environment["HOME"] = "changed caller mapping"
        self.assertEqual(guard.identity, identity)
        self.assertEqual(guard.observation.components[0]["version"], "1")
        guard.require_unchanged()

    def test_mutating_the_observer_result_refuses_instead_of_mutating_baseline(self):
        guard = self.guard()
        self.raw.components[0]["version"] = "2"
        with self.assertRaisesRegex(ValueError, "observation-changed"):
            guard.require_unchanged()

    def test_observer_uses_explicit_environment_without_ambient_loader_injection(self):
        with patch.dict(
            os.environ,
            {"LD_LIBRARY_PATH": "/tmp/ambient", "DYLD_LIBRARY_PATH": "/tmp/ambient"},
        ):
            self.guard()
        values = self.factory.call_args.kwargs
        self.assertEqual(values["windows_environment"], self.environment)
        self.assertEqual(values["toolchain_commands"], (self.binding.command,))
        for name in (
            "linux_loader_paths",
            "freebsd_loader_paths",
            "macos_loader_paths",
        ):
            self.assertEqual(values[name].to_environment(), {})

    def test_changed_command_environment_or_tool_set_refuses_before_observing(self):
        guard = self.guard()
        self.factory.reset_mock()
        for changes in (
            {"environment": {**self.environment, "PATH": "/foreign"}},
            {"tools": ()},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ValueError, "command-context-changed"),
            ):
                guard.require_unchanged(**changes)
        self.factory.assert_not_called()

    def test_missing_substituted_or_extended_tool_bindings_refuse(self):
        changed = LocalComponentToolBinding(
            self.binding.executable,
            ("unreviewed-argument",),
            authority_identity=self.binding.authority_identity,
        )
        for tools in ((), (changed,), (self.binding, self.binding)):
            with (
                self.subTest(tools=tools),
                self.assertRaisesRegex(ValueError, "mismatch"),
            ):
                self.guard(tools=tools)
        self.factory.assert_not_called()

    def test_empty_duplicate_dangling_or_disconnected_graphs_refuse(self):
        ref = "urn:fixture:library"
        for value in (
            object(),
            HostDependencyObservation((), ()),
            HostDependencyObservation(self.raw.components * 2, self.raw.edges),
            HostDependencyObservation(self.raw.components, self.raw.edges * 2),
            HostDependencyObservation(
                self.raw.components, ((self.root_ref, "missing"),)
            ),
            HostDependencyObservation(self.raw.components, ()),
            HostDependencyObservation(
                ({"bom-ref": self.root_ref},), ((self.root_ref, ref),)
            ),
        ):
            self.factory.return_value.observe.return_value = value
            with (
                self.subTest(value=value),
                self.assertRaises(dependencies.RetainedProjectDependencyError),
            ):
                self.guard()

    def test_observer_failure_and_observation_budget_refuse(self):
        self.factory.return_value.observe.side_effect = DependencyObservationError(
            "fixture.unavailable", "cannot inspect"
        )
        with self.assertRaisesRegex(ValueError, "observation-rejected"):
            self.guard()
        self.factory.return_value.observe.side_effect = None
        with patch.object(dependencies, "_MAX_OBSERVATION_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "observation-budget"):
                self.guard()


@unittest.skipUnless(
    os.name != "nt" and shutil.which("cc"), "requires native POSIX fixture"
)
class RetainedDependencyLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        execution_fixtures.RetainedProjectExecutionTests.setUpClass()
        cls.addClassCleanup(
            execution_fixtures.RetainedProjectExecutionTests.doClassCleanups
        )

    def test_only_verified_manifest_selection_can_observe_a_tree_with_source_links(
        self,
    ):
        fixture = execution_fixtures.RetainedProjectExecutionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        (fixture.source / "src/alias").symlink_to("main.txt")
        fixture.manifest = discover_retained_project_inputs(
            fixture.source,
            {"src": "source", "tools": "toolchain", "test-policy.json": "test-policy"},
            fixture.manifest.limits,
            allow_internal_symlinks=True,
            executable_paths=("tools/runner",),
        )
        fixture.plan = replace(
            fixture.plan, manifest_identity=fixture.manifest.identity
        )
        fixture.current = fixture.plan
        tool = (str(fixture.source / "tools/runner"),)
        with self.assertRaisesRegex(DependencyObservationError, "symlinks"):
            PortableHostDependencyObserver(toolchain_commands=(tool,)).observe(
                {"artifact_path": str(fixture.source)}, root_ref="urn:fixture:default"
            )
        empty = PortableHostDependencyObserver(
            toolchain_commands=(tool,), artifact_files=()
        ).observe(
            {"artifact_path": str(fixture.source)},
            root_ref="urn:fixture:explicit-empty",
        )
        self.assertTrue(empty.components)
        workspace = fixture.workspace()
        for _ in fixture.plan.profile.actions:
            workspace.execute_next()
        self.assertTrue((workspace.inputs / "src/alias").is_symlink())
        self.assertTrue(workspace.dependency_observation.components)
