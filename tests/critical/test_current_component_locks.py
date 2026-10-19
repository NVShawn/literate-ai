"""Current typed lock reads use real catalogs and the existing stale-input guard."""

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.component_lock_application import (
    ProjectComponentLockSetError,
    current_component_lock,
    current_component_lock_identity,
    current_project_component_lock_identities,
    current_project_component_locks,
)
from literate_ai.adapters.repository_lineage import FilesystemRepositoryLineageStore
from literate_ai.adapters.retained_project_inputs import (
    discover_retained_project_inputs,
)
from literate_ai.adapters.retained_project_planning import (
    plan_retained_project_with_graph,
    retained_role_identity,
)
from literate_ai.contracts import (
    ContentIdentity,
    ProjectDefinition,
    RepositoryLineage,
    RepositoryParentSelection,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.operator_adoption import ConversionAuthorityStage
from literate_ai.contracts.retained_component_ownership import RetainedComponentOwner
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.contracts.retained_project_tests import (
    RetainedProjectExpectedCase,
    RetainedProjectTestPolicy,
)
from literate_ai.projects import LoadedProject
from tests.support.fixtures_retained_project_sbom import retained_project_sbom_fixture
from tests.support.fixtures_test_cli_component_locks import _run
from tests.support.fixtures_test_component_lock_planning import _fixture


class CurrentComponentLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.component, flavors = _fixture(self.root)
        definition = json.loads(
            (Path(__file__).resolve().parents[2] / "literate.project.json").read_bytes()
        )
        definition["project_id"] = "typed-lock-fixture"
        definition["component_roots"] = ["greeting"]
        definition["default_flavor_selectors"] = []
        definition["test_receipt_policy"]["suite_id"] = "typed-lock-fixture"
        definition["test_receipt_policy"]["minimum_test_count"] = 1
        (self.root / "literate.project.json").write_text(json.dumps(definition))
        self.project = LoadedProject(self.root, ProjectDefinition.from_dict(definition))
        selection = RepositoryParentSelection.root()
        FilesystemRepositoryLineageStore(self.root).replace(
            selection, RepositoryLineage(selection, (), ()), expected_absent=True
        )
        status, record, error = _run(
            [
                "lock",
                str(self.component),
                "--target",
                "host",
                "--flavor",
                "+python",
                "--flavor",
                "+macos",
                "--flavor-root",
                str(flavors),
                "--json",
            ]
        )
        self.assertEqual(status, 0, (record, error))

    def test_typed_and_identity_only_reads_bind_the_same_actual_lock(self):
        lock = current_component_lock(self.component)
        self.assertEqual(current_component_lock_identity(self.component), lock.identity)
        self.assertEqual(current_project_component_locks(self.project), (lock,))
        self.assertEqual(
            current_project_component_lock_identities(self.project), (lock.identity,)
        )

    def test_stale_loaded_project_cannot_supply_graph_metadata(self):
        stale = replace(
            self.project,
            definition=replace(self.project.definition, component_roots=("different",)),
        )
        plan, _locks = retained_project_sbom_fixture()
        binding = SimpleNamespace(require_unchanged=Mock())
        with self.assertRaisesRegex(ValueError, "project-definition-changed"):
            plan_retained_project_with_graph(
                stale,
                binding,
                None,
                plan.profile,
                worker_identity=plan.profile.worker_identity,
                target=plan.profile.target,
                profile_path=self.root / "profile.json",
                profile_bytes=b"{}",
            )
        binding.require_unchanged.assert_not_called()

    @unittest.skipIf(
        os.name == "nt", "POSIX fixture launcher mode; planning executes no launcher"
    )
    def test_planning_bridge_uses_real_locks_and_binary_manifest_under_test_trust(self):
        """Validation/conversion/distribution are explicit unit-test authorities."""
        for name in ("original", "sdk", "build", "tests", "docs"):
            (self.root / name).mkdir()
        (self.root / "original/asset.bin").write_bytes(b"original\x00binary")
        tool = self.root / "sdk/tool"
        tool.write_bytes(b"#!/bin/sh\nexit 0\n")
        tool.chmod(0o755)
        (self.root / "tests/case.txt").write_text("original case")
        (self.root / "docs/manual.txt").write_text("original documentation")
        profile_path = self.root / "build/profile.json"
        profile_path.write_text("{}")
        (self.root / "literate.release.json").write_bytes(
            (Path(__file__).resolve().parents[2] / "literate.release.json").read_bytes()
        )
        policy = RetainedProjectTestPolicy(
            self.project.definition.test_receipt_policy.identity,
            (RetainedProjectExpectedCase("case", "test"),),
            (("test", "test-results/report.json"),),
        )
        policy_bytes = canonical_json_bytes(policy.to_dict())
        (self.root / "test-policy.json").write_bytes(policy_bytes)
        roots = {
            "original": "source",
            "sdk": "toolchain",
            "build": "build-definition",
            "tests": "test-definition",
            "docs": "docs-definition",
            "test-policy.json": "test-policy",
            "literate.release.json": "release-policy",
        }
        limits = RetainedProjectLimits(100, 65536, 262144)
        initial = discover_retained_project_inputs(self.root, roots, limits)
        template, _ = retained_project_sbom_fixture()
        actions = tuple(
            replace(a, outputs=("test-results",)) if a.stage == "test" else a
            for a in template.profile.actions
        )
        profile = replace(
            template.profile,
            toolchain_identity=retained_role_identity(initial, {"sdk", "toolchain"}),
            dependency_identity=retained_role_identity(initial, {"dependency"}),
            test_policy_identity=ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(policy_bytes).hexdigest()
            ),
            release_policy_identity=ContentIdentity.parse_uri(
                "sha256:"
                + hashlib.sha256(
                    (self.root / "literate.release.json").read_bytes()
                ).hexdigest()
            ),
            tools=(
                (
                    "tool",
                    "sdk/tool",
                    ContentIdentity.parse_uri(
                        "sha256:" + hashlib.sha256(tool.read_bytes()).hexdigest()
                    ),
                ),
            ),
            actions=actions,
        )
        profile_bytes = canonical_json_bytes(profile.to_dict())
        profile_path.write_bytes(profile_bytes)
        manifest = discover_retained_project_inputs(self.root, roots, limits)
        binding = SimpleNamespace(
            require_unchanged=Mock(),
            trust_binding=SimpleNamespace(to_dict=lambda: {"test-authority": True}),
        )
        conversion = SimpleNamespace(
            project_id=self.project.definition.project_id,
            stage=ConversionAuthorityStage.RETAINED,
            identity=canonical_identity("test conversion authority"),
        )
        with (
            patch(
                "literate_ai.adapters.retained_project_planning.validated_project_authority_identity",
                return_value=canonical_identity("test validated authority"),
            ),
            patch(
                "literate_ai.adapters.retained_project_planning.FilesystemConversionAuthorityStore.load_optional",
                return_value=conversion,
            ),
        ):
            result = plan_retained_project_with_graph(
                self.project,
                binding,
                manifest,
                profile,
                worker_identity=profile.worker_identity,
                target=profile.target,
                profile_path=profile_path,
                profile_bytes=profile_bytes,
            )
            # Ownership references the actual filesystem catalog and changes the
            # reviewed plan; it does not create a new catalog or execution grant.
            lock = current_project_component_locks(self.project)[0]
            authoring = next(
                n.revision.definition
                for n in lock.nodes
                if n.revision.identity == lock.root_revision
            )
            owned_manifest = replace(
                manifest,
                component_ownership=tuple(
                    RetainedComponentOwner(
                        prefix, authoring.coordinate, authoring.identity
                    )
                    for prefix in sorted(roots)
                ),
            )
            owned_result = plan_retained_project_with_graph(
                self.project,
                binding,
                owned_manifest,
                profile,
                worker_identity=profile.worker_identity,
                target=profile.target,
                profile_path=profile_path,
                profile_bytes=profile_bytes,
            )
            self.assertNotEqual(result.plan.identity, owned_result.plan.identity)
            self.assertEqual(owned_result.owned_components, (authoring.identity,))
            self.assertEqual(
                owned_result.owned_inputs,
                len(manifest.members) + len(manifest.directories),
            )
            self.assertFalse(
                owned_result.to_dict()["component_ownership"]["native_action_admission"]
            )
            self.assertFalse(owned_result.to_dict()["admitted"])
            stale_manifest = replace(
                owned_manifest,
                component_ownership=tuple(
                    replace(
                        owner,
                        authoring_identity=canonical_identity(
                            "changed canonical authority"
                        ),
                    )
                    for owner in owned_manifest.component_ownership
                ),
            )
            with self.assertRaisesRegex(ValueError, "component-missing-or-stale"):
                plan_retained_project_with_graph(
                    self.project,
                    binding,
                    stale_manifest,
                    profile,
                    worker_identity=profile.worker_identity,
                    target=profile.target,
                    profile_path=profile_path,
                    profile_bytes=profile_bytes,
                )
        self.assertEqual(
            result.plan.component_locks,
            current_project_component_lock_identities(self.project),
        )
        root = next(c for c in result.managed_sbom_graph.components if c.kind.is_root)
        self.assertEqual(root.version, self.project.definition.version)
        self.assertEqual(root.identity, result.plan.identity)
        self.assertFalse(result.to_dict()["admitted"])
        self.assertFalse((self.root / "verification/current.json").exists())

    def unselected(self):
        target = self.root / "unselected"
        shutil.copytree(
            self.component, target, ignore=shutil.ignore_patterns("component.lock.json")
        )
        project = replace(
            self.project,
            definition=replace(
                self.project.definition, component_roots=("greeting", "unselected")
            ),
        )
        (self.root / "literate.project.json").write_text(
            json.dumps(project.definition.to_dict())
        )
        return target, project

    def test_unlocked_catalog_component_does_not_become_a_selected_lock(self):
        _target, project = self.unselected()
        self.assertEqual(
            current_project_component_locks(project),
            current_project_component_locks(self.project),
        )

    def test_invalid_persisted_lock_is_not_treated_as_unselected(self):
        target, project = self.unselected()
        (target / "component.lock.json").write_text("{}\n")
        with self.assertRaises(ProjectComponentLockSetError):
            current_project_component_locks(project)

    def test_changed_specification_refuses_both_current_read_apis(self):
        path = self.component / "specs/spec.md"
        path.write_text(path.read_text() + "\nChanged original authority.\n")
        for reader in (current_component_lock, current_component_lock_identity):
            with (
                self.subTest(reader=reader),
                self.assertRaises(ProjectComponentLockSetError),
            ):
                reader(self.component)
