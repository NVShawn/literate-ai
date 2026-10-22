"""Bind retained manifests and native observations without origin substitution."""

import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.dependencies import validate_cyclonedx_bom
from literate_ai.adapters.dependencies.types import HostDependencyObservation
from literate_ai.adapters.retained_project_boms import (
    RetainedProjectResolvedBom,
    RetainedProjectSourceBom,
    resolve_retained_project_bom,
)
from literate_ai.adapters.retained_project_inputs import (
    discover_retained_project_inputs,
)
from literate_ai.contracts import (
    ContentIdentity,
    CycloneDxLifecycle,
    canonical_identity,
)
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.contracts.retained_project_sbom import (
    project_retained_project_managed_graph,
)
from tests.critical import test_retained_project_execution as execution_fixtures
from tests.support.fixtures_retained_project_sbom import retained_project_sbom_fixture


class RetainedProjectBomTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name in ("src", "sdk", "dependencies", "exports"):
            (self.root / name).mkdir()
        (self.root / "src/main.txt").write_bytes(b"retained\x00asset")
        (self.root / "sdk/tool").write_bytes(b"declared-tool-fixture")
        (self.root / "sdk/tool").chmod(0o755)
        (self.root / "dependencies/input.dat").write_bytes(b"declared dependency")
        (self.root / "exports/app").write_bytes(b"build output")
        limits = RetainedProjectLimits(100, 1024**2, 10 * 1024**2)
        self.manifest = discover_retained_project_inputs(
            self.root,
            {"src": "source", "sdk": "sdk", "dependencies": "dependency"},
            limits,
            executable_paths=("sdk/tool",),
        )
        self.outputs = discover_retained_project_inputs(
            self.root / "exports",
            {"app": "retained-output"},
            limits,
        )
        plan, self.locks = retained_project_sbom_fixture()
        tool = next(
            member for member in self.manifest.members if member.path == "sdk/tool"
        )
        self.plan = replace(
            plan,
            manifest_identity=self.manifest.identity,
            profile=replace(
                plan.profile,
                tools=(
                    (
                        "tool",
                        "sdk/tool",
                        ContentIdentity.parse_uri("sha256:" + tool.sha256),
                    ),
                ),
                toolchain_identity=self.manifest.role_identity({"sdk", "toolchain"}),
                dependency_identity=self.manifest.role_identity({"dependency"}),
            ),
        )
        self.graph = project_retained_project_managed_graph(
            self.plan, self.locks, version="1.0.0"
        )
        self.source = RetainedProjectSourceBom(
            self.plan, self.manifest, self.graph, self.locks
        )
        # A schema/transition fixture, not a claim that these are native SDK bytes.
        self.component = {
            "type": "file",
            "bom-ref": "urn:fixture:native-library",
            "name": "native-library",
            "version": "sha256-" + "a" * 64,
            "hashes": [{"alg": "SHA-256", "content": "a" * 64}],
            "properties": [
                {"name": "literate-ai:dependency-kind", "value": "system"},
                {"name": "literate-ai:dependency-scope", "value": "system"},
            ],
        }
        self.observed = HostDependencyObservation(
            (self.component,), ((self.graph.root_ref, self.component["bom-ref"]),)
        )

    def resolve(self, exports=None):
        return resolve_retained_project_bom(
            self.source,
            self.outputs,
            self.observed,
            self.observed if exports is None else exports,
        )

    def test_source_and_resolution_validate_through_existing_cyclonedx_owner(self):
        resolved = self.resolve()
        self.assertIsInstance(resolved, RetainedProjectResolvedBom)
        self.assertEqual(
            validate_cyclonedx_bom(
                resolved.content,
                lifecycle=CycloneDxLifecycle.RESOLVED,
                managed_graph=self.graph,
                source_content=self.source.content,
            ),
            resolved.binding,
        )
        self.assertEqual(
            resolved.binding.source_bom_identity, self.source.binding.bom_identity
        )
        source = json.loads(self.source.content)
        result = json.loads(resolved.content)
        properties = source["metadata"]["component"]["properties"]
        self.assertIn(
            {"name": "literate-ai:retained-project", "value": self.plan.identity.uri},
            properties,
        )
        self.assertNotIn(
            "literate-ai:component-revision", {p["name"] for p in properties}
        )
        self.assertTrue(
            any(c["name"] == "retained-input-manifest" for c in source["components"])
        )
        self.assertTrue(
            any(c["name"] == "retained-output-manifest" for c in result["components"])
        )
        self.assertIsInstance(resolved.identity, ContentIdentity)

    def test_input_graph_role_and_tool_substitution_refuse(self):
        for changes in (
            {"manifest_identity": canonical_identity("other inputs")},
            {
                "profile": replace(
                    self.plan.profile,
                    toolchain_identity=canonical_identity("other SDK"),
                )
            },
            {
                "profile": replace(
                    self.plan.profile,
                    dependency_identity=canonical_identity("other deps"),
                )
            },
            {
                "profile": replace(
                    self.plan.profile,
                    tools=(("tool", "sdk/tool", canonical_identity("other tool")),),
                )
            },
        ):
            changed = replace(self.plan, **changes)
            graph = project_retained_project_managed_graph(
                changed, self.locks, version="1.0.0"
            )
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                RetainedProjectSourceBom(changed, self.manifest, graph, self.locks)
        other = replace(self.plan, project_id="other project")
        other_graph = project_retained_project_managed_graph(
            other, self.locks, version="1.0.0"
        )
        with self.assertRaisesRegex(ValueError, "source-authority"):
            RetainedProjectSourceBom(self.plan, self.manifest, other_graph, self.locks)

    def test_document_byte_substitution_and_invalid_transition_refuse(self):
        resolved = self.resolve()
        for content in (self.source.content + b"\n", resolved.content, b"{}"):
            with self.subTest(content=content[:20]), self.assertRaises(ValueError):
                self.source.require_content(content)
        with self.assertRaises(ValueError):
            resolved.require_content(resolved.content + b"\n")
        with self.assertRaises(ValueError):
            replace(resolved, tool_observation=b'{"components":[],"edges":[]}')

    def test_managed_nodes_must_match_the_actual_selected_component_locks(self):
        altered = replace(
            self.graph,
            components=tuple(
                component
                if component.kind.is_root
                else replace(component, name="forged locked name")
                for component in self.graph.components
            ),
        )
        with self.assertRaisesRegex(ValueError, "selected-lock-graph"):
            RetainedProjectSourceBom(self.plan, self.manifest, altered, self.locks)
        for locks in (self.locks[:1], self.locks[::-1], ()):
            with self.subTest(locks=locks), self.assertRaises(ValueError):
                RetainedProjectSourceBom(self.plan, self.manifest, self.graph, locks)

    def test_mutable_observer_data_cannot_rewrite_retained_bom_custody(self):
        resolved = self.resolve()
        identity, content = resolved.identity, resolved.content
        self.component["version"] = "changed caller mapping"
        self.assertEqual(resolved.identity, identity)
        self.assertEqual(resolved.content, content)

    def test_conflicting_native_observations_cannot_merge(self):
        changed = {**self.component, "version": "other native version"}
        exports = HostDependencyObservation((changed,), self.observed.edges)
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.resolve(exports)

    def test_data_exports_may_have_no_native_images_but_tools_must_be_observed(self):
        self.resolve(HostDependencyObservation((), ()))
        with self.assertRaises(ValueError):
            resolve_retained_project_bom(
                self.source,
                self.outputs,
                HostDependencyObservation((), ()),
                self.observed,
            )

    def test_native_graph_duplicates_dangling_nodes_and_managed_claims_refuse(self):
        for observed in (
            HostDependencyObservation(
                self.observed.components * 2, self.observed.edges
            ),
            HostDependencyObservation(
                self.observed.components, ((self.graph.root_ref, "missing"),)
            ),
            HostDependencyObservation(self.observed.components, ()),
            HostDependencyObservation(
                (
                    {
                        **self.component,
                        "properties": [
                            *self.component["properties"],
                            {
                                "name": "literate-ai:retained-project",
                                "value": self.plan.identity.uri,
                            },
                        ],
                    },
                ),
                self.observed.edges,
            ),
        ):
            with (
                self.subTest(observed=observed),
                self.assertRaises((ValueError, RuntimeError)),
            ):
                self.resolve(observed)

    def test_outputs_change_resolution_identity_without_rewriting_source_intent(self):
        first = self.resolve()
        (self.root / "exports/app").write_bytes(b"new output")
        changed = discover_retained_project_inputs(
            self.root / "exports", {"app": "retained-output"}, self.outputs.limits
        )
        second = resolve_retained_project_bom(
            self.source, changed, self.observed, self.observed
        )
        self.assertEqual(first.source.binding, second.source.binding)
        self.assertNotEqual(first.binding, second.binding)
        self.assertNotEqual(first.identity, second.identity)


@unittest.skipUnless(
    os.name != "nt" and shutil.which("cc"), "requires native POSIX fixture"
)
class RetainedProjectBomExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        execution_fixtures.RetainedProjectExecutionTests.setUpClass()
        cls.addClassCleanup(
            execution_fixtures.RetainedProjectExecutionTests.doClassCleanups
        )

    def setUp(self):
        self.fixture = execution_fixtures.RetainedProjectExecutionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        _plan, locks = retained_project_sbom_fixture()
        profile = replace(
            self.fixture.profile,
            toolchain_identity=self.fixture.manifest.role_identity(
                {"sdk", "toolchain"}
            ),
            dependency_identity=self.fixture.manifest.role_identity({"dependency"}),
        )
        self.fixture.plan = replace(
            self.fixture.plan,
            profile=profile,
            component_locks=tuple(lock.identity for lock in locks),
        )
        self.fixture.current = self.fixture.plan
        self.locks = locks
        self.graph = project_retained_project_managed_graph(
            self.fixture.plan, locks, version="1.0.0"
        )

    def test_real_command_build_has_resolved_bom_before_tests(self):
        workspace = self.fixture.workspace(
            managed_graph=self.graph, component_locks=self.locks
        )
        self.assertIsNotNone(workspace.source_bom)
        self.assertIsNone(workspace.resolved_bom)
        workspace.execute_next()
        resolved = workspace.resolved_bom
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.source.plan, self.fixture.plan)
        self.assertEqual(
            tuple(member.path for member in resolved.output_manifest.members),
            ("app.bin",),
        )
        self.assertEqual(len(workspace.test_reports), 0)
        for _ in range(3):
            workspace.execute_next()
        self.assertIs(workspace.resolved_bom, resolved)
        self.assertEqual(len(workspace.test_reports), 1)
        self.assertIsInstance(resolved.identity, ContentIdentity)

    def test_unrelated_graph_refuses_before_workspace_creation(self):
        _plan, locks = retained_project_sbom_fixture()
        changed = replace(self.fixture.plan, project_id="different project")
        graph = project_retained_project_managed_graph(changed, locks, version="1.0.0")
        with self.assertRaisesRegex(ValueError, "source-authority"):
            self.fixture.workspace(managed_graph=graph, component_locks=self.locks)
        self.assertEqual(list(self.fixture.base.glob("retained-run-*")), [])

    def test_graph_and_lock_authority_cannot_be_selected_independently(self):
        for values in ({"managed_graph": self.graph}, {"component_locks": self.locks}):
            with (
                self.subTest(values=values),
                self.assertRaisesRegex(ValueError, "required-together"),
            ):
                self.fixture.workspace(**values)

    def test_compiled_fixture_export_has_native_resolution_before_tests(self):
        actions = list(self.fixture.plan.profile.actions)
        actions[0] = replace(actions[0], argv=("export", "{output_root}/app.bin"))
        report = json.dumps(
            {
                "schema": "literate-ai/retained-project-test-report@1",
                "action": "tests",
                "cases": [
                    {"id": "original-suite", "outcome": "passed", "reason": None}
                ],
            }
        )
        actions[1] = replace(
            actions[1], argv=("report", "{output_root}/test-result.json", report)
        )
        self.fixture.plan = replace(
            self.fixture.plan,
            profile=replace(self.fixture.plan.profile, actions=tuple(actions)),
        )
        self.fixture.current = self.fixture.plan
        graph = project_retained_project_managed_graph(
            self.fixture.plan, self.locks, version="1.0.0"
        )
        workspace = self.fixture.workspace(
            managed_graph=graph, component_locks=self.locks
        )
        workspace.execute_next()
        resolved = workspace.resolved_bom
        self.assertTrue(json.loads(resolved.export_observation)["components"])
        self.assertEqual(
            resolved.binding.source_bom_identity,
            workspace.source_bom.binding.bom_identity,
        )
        self.assertEqual(len(workspace.test_reports), 0)
        for _ in range(3):
            workspace.execute_next()
        self.assertEqual(len(workspace.test_reports), 1)
