"""Current provider admission composes real files with measured installed fixtures."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters.authority import FileAuthorityProjectionStore
from literate_ai.adapters.qualification_authority import qualification_verifier_case_map
from literate_ai.adapters.repository_lineage import FilesystemRepositoryLineageStore
from literate_ai.adapters.retained_provider_authority import (
    read_retained_provider_authority,
)
from literate_ai.adapters.standard_lifecycle_binding import (
    StandardLifecycleBindingError,
    resolve_standard_project_lifecycle_driver,
)
from literate_ai.authority import ComponentAuthorityLifecycle
from literate_ai.contracts import (
    ContentIdentity,
    RepositoryLineage,
    RepositoryParentSelection,
    StandardProjectLifecycleDriver,
    canonical_identity,
    load_current_standard_lifecycle_policy,
)
from literate_ai.projects import PinnedInputClosureError, ProjectConfigurationStore
from literate_ai.source_to_specification.host_qualification import (
    LocalQualificationCase,
    LocalQualificationProfile,
)
from literate_ai.source_to_specification.promotion_materialization import (
    SOURCE_PROMOTION_PROVENANCE_SCHEMA,
    SourcePromotionError,
)
from tests.support import fixtures_test_qualification_lifecycle_runner as lifecycle_fixtures
from tests.support import fixtures_test_retained_provider_generation as generation_fixtures
from tests.support import fixtures_test_standard_lifecycle_binding_adapter as binding_fixtures
from tests.support.fixtures_test_project_configuration import _definition


class RetainedProviderAuthorityTests(unittest.TestCase):
    generation_fixture_class = generation_fixtures.RetainedProviderGenerationTests

    def setUp(self):
        self.fixture = self.generation_fixture_class("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root.resolve()
        installed = tempfile.TemporaryDirectory()
        self.addCleanup(installed.cleanup)
        self.distribution = binding_fixtures.FakeDistribution(Path(installed.name))
        self.policy = load_current_standard_lifecycle_policy()
        self.observation = binding_fixtures.observe(self.distribution)
        self.driver = StandardProjectLifecycleDriver(
            self.observation.identity, self.policy.identity
        )
        definition = replace(
            _definition(), lifecycle_driver=self.driver, component_roots=("greeting",)
        )
        ProjectConfigurationStore(self.root).create(definition)
        parent = RepositoryParentSelection.root()
        FilesystemRepositoryLineageStore(self.root).replace(
            parent, RepositoryLineage(parent, (), ()), expected_absent=True
        )
        generation_fixtures.fixtures._write_lock(
            self.fixture.component, self.fixture.flavors
        )
        self.generation = self.fixture.read()
        authority = self.generation.prepared.locked_authority_snapshot.authority
        self.authority = authority
        source = canonical_identity({"reviewed-source": 1})
        self.profile = LocalQualificationProfile(
            "provider@1",
            ("build",),
            (("test",),),
            ("baseline",),
            ("generated",),
            (LocalQualificationCase("known", ({"n": 1},), {"n": 2}),),
            ("api.value",),
        )
        self.profile_path = self.root / "profile.json"
        self.profile_path.write_text(json.dumps(self.profile.to_dict()))
        evidence = self.fixture.audit_evidence(omit_prefix=".literate/")
        audit = evidence.promotion_input_audits[0]
        case_map = qualification_verifier_case_map(self.profile, source)
        runner = lifecycle_fixtures.QualificationLifecycleRunnerTests("runTest")
        runner.setUp()
        root_node = next(
            n
            for n in authority.lock.nodes
            if n.revision.identity == authority.lock.root_revision
        )
        plan = replace(
            runner.plan,
            target_profile_identity=authority.lock.target_profile_identity,
            component_lock_identity=authority.lock.identity,
            specification_set_identity=root_node.revision.specification_set_identity,
            source_snapshot_identity=source,
            case_map=case_map,
            generation_input_audit_identity=ContentIdentity.parse_uri(audit.identity),
            promotion_tree_identity=ContentIdentity.parse_uri(
                audit.materialized_tree_identity
            ),
        )
        result = runner.runner(verifier=lifecycle_fixtures.Verifier([], case_map)).run(
            plan
        )
        result = replace(
            result,
            runs=tuple(
                replace(
                    run,
                    driver_identity=self.driver.identity,
                    lifecycle_policy_identity=self.policy.identity,
                    framework_distribution_identity=self.observation.identity,
                )
                for run in result.runs
            ),
        )
        closure = self.generation.generation_closure(
            self.root, evidence, qualification_identity=result.identity
        )
        audit_name = f"generation-input-audits/{audit.identity.split(':')[1]}.json"
        record = {
            "schema": SOURCE_PROMOTION_PROVENANCE_SCHEMA,
            "source_snapshot_identity": source.uri,
            "specification_set_identity": (
                root_node.revision.specification_set_identity.uri
            ),
            "review_identity": canonical_identity({"human-review": 1}).uri,
            "component_graph_identity": canonical_identity({"graph": 1}).uri,
            "translation_identity": None,
            "inverse_evidence_reference": None,
            "generation_input_audit_references": [
                {
                    "kind": "generation-input-audit",
                    "identity": audit.identity,
                    "path": audit_name,
                }
            ],
        }
        provenance_id = canonical_identity(record)
        provenance = self.root / "provenance/source-promotion" / provenance_id.digest
        (provenance / "generation-input-audits").mkdir(parents=True)
        (provenance / "reference.json").write_text(json.dumps(record))
        (provenance / audit_name).write_text(json.dumps(audit.to_dict()))
        directory = self.root / "provenance/qualification"
        directory.mkdir()
        (directory / f"{result.identity.digest}.json").write_text(
            json.dumps(result.to_dict())
        )
        inventory = ComponentAuthorityLifecycle.inventory(
            component_coordinate=authority.root_authoring.coordinate.uri,
            source_snapshot_identity=source,
            provenance_reference_identity=provenance_id,
            evidence_identities=(canonical_identity({"inventory": 1}),),
        )
        derived = ComponentAuthorityLifecycle.derive(
            inventory,
            component_revision_identity=authority.lock.root_revision,
            specification_set_identity=root_node.revision.specification_set_identity,
            evidence_identities=(canonical_identity({"derived": 1}),),
        )
        retained = ComponentAuthorityLifecycle.accept(
            derived, evidence_identities=(canonical_identity({"accepted": 1}),)
        )
        store = FileAuthorityProjectionStore(self.root)
        for projection in (inventory, derived, retained):
            store.append(projection)
        store.append_qualified(
            authority,
            replace(
                evidence,
                authority_projection=retained,
                target_lock_identity=authority.lock.identity,
                generation_closure=closure,
                verifier_identity=case_map.verifier_identity,
                policy_identity=self.policy.identity,
                qualification_lifecycle_result=result,
            ),
        )
        self.binding_patch = mock.patch(
            "literate_ai.adapters.retained_provider_authority.resolve_standard_project_lifecycle_driver",
            side_effect=lambda driver: resolve_standard_project_lifecycle_driver(
                driver,
                distribution_observer=lambda: binding_fixtures.observe(
                    self.distribution
                ),
                policy_loader=lambda: self.policy,
            ),
        )
        self.binding_patch.start()
        self.addCleanup(self.binding_patch.stop)

    def read(self):
        return read_retained_provider_authority(
            self.root,
            self.fixture.request,
            provider_id="codex",
            profile_path="profile.json",
        )

    def inventory(self):
        return generation_fixtures._inventory(self.root)

    def test_composed_current_admission_preserves_all_provider_files(self):
        before = self.inventory()
        with mock.patch("subprocess.Popen", side_effect=AssertionError("execution")):
            current = self.read()
            current.require_unchanged()
        self.assertEqual(
            current.promotion.generation_closure,
            current.promotion.evidence.generation_closure,
        )
        self.assertEqual(current.lifecycle.driver, self.driver)
        self.assertEqual(
            set(current.generation.recipes),
            {n.revision.identity for n in self.authority.lock.nodes},
        )
        self.assertEqual(before, self.inventory())

    def test_installed_payload_drift_refuses_guard_and_fresh_admission(self):
        current = self.read()
        path = self.distribution.locations["literate_ai/__init__.py"]
        path.write_bytes(b"changed installed payload\n")
        with self.assertRaises(StandardLifecycleBindingError):
            current.require_unchanged()
        with self.assertRaises(StandardLifecycleBindingError):
            self.read()

    def test_current_profile_drift_refuses_guard_and_fresh_admission(self):
        current = self.read()
        self.profile_path.write_text(
            json.dumps(replace(self.profile, timeout_seconds=901).to_dict())
        )
        with self.assertRaises(PinnedInputClosureError):
            current.require_unchanged()
        with self.assertRaises(SourcePromotionError):
            self.read()

    def test_project_configuration_bytes_remain_guarded(self):
        current = self.read()
        path = self.root / "literate.project.json"
        changed = path.read_bytes() + b"\n"
        path.write_bytes(changed)
        with self.assertRaises(PinnedInputClosureError):
            current.require_unchanged()
        self.assertEqual(path.read_bytes(), changed)

    def test_installed_drift_during_promotion_guard_prevents_return(self):
        current = self.read()
        promotion_type = type(current.promotion)
        verify = promotion_type.require_unchanged
        path = self.distribution.locations["literate_ai/__init__.py"]

        def change_installed_after_promotion(promotion):
            verify(promotion)
            path.write_bytes(b"changed while reopening promotion\n")

        with (
            mock.patch.object(
                promotion_type, "require_unchanged", change_installed_after_promotion
            ),
            self.assertRaises(StandardLifecycleBindingError),
        ):
            current.require_unchanged()
        self.assertEqual(path.read_bytes(), b"changed while reopening promotion\n")
