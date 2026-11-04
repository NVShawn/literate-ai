"""Current typed lock reads use real catalogs and the existing stale-input guard."""

import hashlib
import json
import os
import shutil
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
from literate_ai.adapters.locked_generation_authority import (
    FilesystemLockedGenerationAuthorityReader,
    LockedGenerationAuthorityReaderError,
    read_only_component_catalog_observations,
)
from literate_ai.adapters.retained_project_inputs import (
    discover_retained_project_inputs,
)
from literate_ai.adapters.retained_project_planning import (
    plan_retained_project_with_graph,
    retained_role_identity,
)
from literate_ai.contracts import (
    ContentIdentity,
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
from literate_ai.projects import ProjectError
from tests.support.fixtures_current_component_locks import (
    initialize_current_component_lock_fixture,
)
from tests.support.fixtures_retained_project_sbom import retained_project_sbom_fixture
from tests.support.fixtures_test_cli_component_locks import _run


class CurrentComponentLockTests(unittest.TestCase):
    def setUp(self):
        initialize_current_component_lock_fixture(self)

    def test_typed_and_identity_only_reads_bind_the_same_actual_lock(self):
        lock = current_component_lock(self.component)
        self.assertEqual(current_component_lock_identity(self.component), lock.identity)
        self.assertEqual(current_project_component_locks(self.project), (lock,))
        self.assertEqual(
            current_project_component_lock_identities(self.project), (lock.identity,)
        )

    def test_shared_catalog_keeps_each_lock_and_rejects_later_source_drift(self):
        catalog_root = self.root / "catalog"
        catalog_root.mkdir()
        original = catalog_root / "greeting"
        self.component.rename(original)
        self.component = original
        target = catalog_root / "second"
        shutil.copytree(original, target)
        authoring = target / "component.md"
        authoring.write_text(
            authoring.read_text().replace("version: 1.0.0", "version: 1.0.1", 1)
        )
        project = replace(
            self.project,
            definition=replace(self.project.definition, component_roots=("catalog",)),
        )
        (self.root / "literate.project.json").write_text(
            json.dumps(project.definition.to_dict())
        )
        for component in (original, target):
            status, record, error = _run(
                [
                    "lock",
                    str(component),
                    "--target",
                    "host",
                    "--flavor",
                    "+python",
                    "--flavor",
                    "+macos",
                    "--json",
                ]
            )
            self.assertEqual(status, 0, (record, error))
        expected = tuple(
            sorted(
                (
                    current_component_lock(self.component),
                    current_component_lock(target),
                ),
                key=lambda item: item.identity.uri,
            )
        )
        with read_only_component_catalog_observations():
            self.assertEqual(current_project_component_locks(project), expected)
            specification = target / "specs/spec.md"
            specification.write_text(specification.read_text() + "\nChanged source.\n")
            with self.assertRaises(ProjectComponentLockSetError):
                current_project_component_locks(project)

    def test_reused_catalog_rechecks_bytes_membership_and_nearest_project(self):
        with read_only_component_catalog_observations():
            reader = FilesystemLockedGenerationAuthorityReader()
            catalog = reader.load_catalog(self.component)
            observed = reader.load_catalog(self.component)
            self.assertEqual(
                observed.catalog_audit_identity, catalog.catalog_audit_identity
            )
            flavor = self.root / "flavors/lang-python/flavor.md"
            original = flavor.read_bytes()
            try:
                flavor.write_bytes(original + b"\nChanged flavor authority.\n")
                with self.assertRaises(LockedGenerationAuthorityReaderError):
                    reader.load_catalog(self.component)
            finally:
                flavor.write_bytes(original)
            added = self.component / "added/component.md"
            added.parent.mkdir()
            try:
                added.write_bytes((self.component / "component.md").read_bytes())
                with self.assertRaises(LockedGenerationAuthorityReaderError):
                    reader.load_catalog(self.component)
            finally:
                added.unlink()
                added.parent.rmdir()
            if os.name != "nt":
                retired = flavor.with_name("flavor-original.md")
                flavor.rename(retired)
                try:
                    flavor.symlink_to(retired.name)
                    with self.assertRaises(LockedGenerationAuthorityReaderError):
                        reader.load_catalog(self.component)
                finally:
                    flavor.unlink(missing_ok=True)
                    retired.rename(flavor)
            # A new nearest project must be discovered, rather than inheriting the
            # old valid parent catalog across a now-invalid authority boundary.
            (self.component / "literate.project.json").write_text("{}\n")
            with self.assertRaises(ProjectError):
                reader.load_catalog(self.component)

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
            # Original-source authority may plan the same locked retained inputs,
            # but its distinct initial choice changes the reviewed plan identity.
            conversion.stage = ConversionAuthorityStage.ORIGINAL
            conversion.identity = canonical_identity("original authority")
            original_result = plan_retained_project_with_graph(
                self.project,
                binding,
                manifest,
                profile,
                worker_identity=profile.worker_identity,
                target=profile.target,
                profile_path=profile_path,
                profile_bytes=profile_bytes,
            )
            self.assertNotEqual(result.plan.identity, original_result.plan.identity)
            self.assertFalse(original_result.to_dict()["admitted"])
            conversion.stage = ConversionAuthorityStage.RETAINED
            conversion.identity = canonical_identity("test conversion authority")
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
