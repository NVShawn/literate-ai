"""Public qualification routing and oracle binding without generation.

Authority preparation, accepted-run results and final admission are injected
boundaries. The CLI, qualification adapter construction, verifier-file loading,
case binding, failure envelope and result-file projection are real.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.component_acceptance import LIBRARY_SCHEMA, LibraryAcceptance
from literate_ai.authority import ComponentAuthorityState
from literate_ai.contracts import ComponentCoordinate, ContentIdentity
from literate_ai.source_to_specification.host_qualification import (
    LocalQualificationCase,
    LocalQualificationProfile,
)
from literate_ai.source_to_specification.inventory import inventory_source
from tests.unit.test_cli_rebuild import invoke
from tests.unit.test_standard_rebuild_adapter import (
    _identity as identity,
)


class PublicLibraryQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.unit.test_standard_rebuild_adapter import (
            FilesystemStandardRebuildAdapterTests as RebuildFixture,
        )

        self.fixture = RebuildFixture("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.project = self.fixture.project
        self.project.definition.flavor_roots = ()
        (self.project.root / "component.md").write_bytes(b"# Fixture library\n")
        self.source = self.fixture.root / "baseline"
        self.source.mkdir()
        (self.source / "library.py").write_bytes(b"def value(): return True\n")
        source_identity = ContentIdentity.parse_uri(
            inventory_source(self.source).identity
        )
        self.coordinate = ComponentCoordinate("fixture", "library")
        self.specification = identity("specification")
        revision = SimpleNamespace(
            identity=self.fixture.invalidation.changed_component,
            specification_set_identity=self.specification,
            definition=SimpleNamespace(coordinate=self.coordinate),
        )
        lock = self.fixture.snapshot.authority.lock
        lock.nodes = (SimpleNamespace(revision=revision),)
        lock.root_revision = revision.identity
        lock.target_profile_identity = identity("target")
        self.fixture.snapshot.authority.root_authoring = SimpleNamespace(
            resolved_kind="library", coordinate=self.coordinate
        )
        self.projection = SimpleNamespace(
            identity=identity("projection"),
            specification_set_identity=self.specification,
            source_snapshot_identity=source_identity,
        )
        self.profile = LocalQualificationProfile(
            "library-qualification@1",
            ("build",),
            (("test",),),
            ("source",),
            ("generated",),
            (LocalQualificationCase("known", ({"nested": [True]},), {"value": True}),),
            ("api.value",),
        )
        self.profile_path = self.project.root / "profile.json"
        self.write_profile(self.profile)
        oracle_root = self.project.root / "verification" / "acceptance"
        oracle_root.mkdir(parents=True)
        harness = b"# reviewed harness fixture; never executed\n"
        (oracle_root / "harness.py").write_bytes(harness)
        oracle = {
            "schema": LIBRARY_SCHEMA,
            "component": "library",
            "specification_set_identity": self.specification.uri,
            "public_interface_identities": [identity("interface").uri],
            "import_surface_identity": identity("surface").uri,
            "language": "python",
            "harness": "harness.py",
            "harness_identity": "sha256:" + hashlib.sha256(harness).hexdigest(),
            "cases": [
                {
                    "case_id": "known",
                    "capability": "api.value",
                    "arguments": [{"nested": [True]}],
                    "expected_result": {"value": True},
                }
            ],
        }
        (oracle_root / "library.json").write_text(json.dumps(oracle), encoding="utf-8")
        self.output = self.fixture.root / "qualification.json"
        self.authority_store = Mock()
        self.authority_store.current.return_value = self.projection
        audit = SimpleNamespace(
            identity=identity("audit").uri,
            materialized_tree_identity=identity("promotion").uri,
        )
        verified = SimpleNamespace(
            audit_containing=lambda _: audit,
            translation={"mode": "deterministic-static"},
            translation_identity=None,
            inverse_evidence_custody=None,
            inverse_evidence_custody_identity=None,
            promotion_input_audits=(),
        )
        self.verified_promotion = verified
        self.plan = {
            "component": {"revision_identity": revision.identity.uri},
            "resolution": {
                "component_lock_identity": lock.identity.uri,
                "target_profile_identity": lock.target_profile_identity.uri,
            },
            "recipe_identity": identity("recipe").uri,
            "selected_flavors": [],
            "workflow": {"reference": {"identity": identity("workflow").to_dict()}},
            "routing": {"reference": {"identity": identity("routing").to_dict()}},
        }
        self.stack = self.enterContext(ExitStack())
        for target, value in (
            ("literate_ai.cli.source_to_specification.discover_project", self.project),
            (
                "literate_ai.adapters.locked_generation_authority.FilesystemLockedGenerationAuthorityReader.read",
                self.fixture.snapshot,
            ),
            (
                "literate_ai.adapters.authority.FileAuthorityProjectionStore",
                self.authority_store,
            ),
            (
                "literate_ai.authority.ComponentAuthorityLifecycle.status",
                SimpleNamespace(
                    recorded_state=ComponentAuthorityState.DERIVED_SOURCE_RETAINED,
                    blockers=("regenerative-qualification-required",),
                ),
            ),
            (
                "literate_ai.application.SourcePromotionService.verify_evidence",
                verified,
            ),
            (
                "literate_ai.application.SourcePromotionService.assess_source_exclusion",
                SimpleNamespace(source_excluded=True),
            ),
            ("literate_ai.cli.generation.plan_from_args", self.plan),
            (
                "literate_ai.cli.source_to_specification.generation_input_subset_identity",
                identity("subset"),
            ),
            (
                "literate_ai.adapters.standard_lifecycle_binding.resolve_standard_project_lifecycle_driver",
                self.fixture.binding,
            ),
            (
                "literate_ai.adapters.generation_preparation.FilesystemLockedGenerationApplicationAdapter.prepare",
                self.fixture.prepared,
            ),
            (
                "literate_ai.source_to_specification.VerifiedSourcePromotionEvidence",
                object(),
            ),
        ):
            self.stack.enter_context(patch(target, return_value=value))
        self.qualify = self.stack.enter_context(
            patch(
                "literate_ai.adapters.qualification.FilesystemStandardQualificationAdapter.qualify",
                autospec=True,
                side_effect=self.accepted_runs,
            )
        )

    def write_profile(self, profile):
        self.profile_path.write_text(json.dumps(profile.to_dict()), encoding="utf-8")

    def accepted_runs(self, adapter, plan):
        oracle = adapter.lifecycle.acceptance_oracle
        self.assertIsInstance(oracle, LibraryAcceptance)
        self.assertEqual(oracle.import_surface_identity, identity("surface"))
        self.assertEqual(oracle.specification_set_identity, self.specification)
        self.assertEqual(oracle.cases[0].arguments, [{"nested": [True]}])
        self.assertEqual(len(plan.run_identities), 2)
        binding = self.fixture.binding
        run = SimpleNamespace(
            driver_identity=binding.driver.identity,
            lifecycle_policy_identity=binding.policy.identity,
            framework_distribution_identity=binding.distribution.identity,
        )
        return SimpleNamespace(
            identity=identity("injected-accepted-runs"),
            case_map=plan.case_map,
            runs=(run, run),
            to_dict=lambda: {"fixture": "accepted-two-run-result"},
        )

    def invoke(self, *extra):
        return invoke(
            "spec",
            "qualify",
            str(self.project.root),
            "--source",
            str(self.source),
            "--profile",
            str(self.profile_path),
            "--output",
            str(self.output),
            "--allow-host-execution",
            *extra,
        )

    def retained_runs(self, adapter, plan):
        from literate_ai.contracts.identity import (
            canonical_identity,
            canonical_json_bytes,
        )

        result = self.accepted_runs(adapter, plan)
        result.identity = canonical_identity(result.to_dict())
        payload = canonical_json_bytes(result.to_dict())
        adapter.evidence_blobs = ((result.identity, payload),)
        adapter.library_captures = tuple(
            SimpleNamespace(
                run=SimpleNamespace(run_identity=run),
                exports=SimpleNamespace(identity=identity("exports")),
            )
            for run in plan.run_identities
        )
        self.retained_scratch = adapter.workspaces.session_root.parent
        return result

    def test_explicit_retention_publishes_reopenable_archive_before_cleanup(self):
        import shutil

        from literate_ai.adapters.evidence_storage import FileSystemEvidenceStore
        from literate_ai.adapters.qualification_archive import (
            reopen_qualification_archive,
        )
        from literate_ai.contracts.blobs import BlobRef
        from literate_ai.contracts.identity import ContentIdentity

        store_path = self.fixture.root / "evidence"
        self.qualify.side_effect = self.retained_runs
        original_cleanup = shutil.rmtree
        cleanup_observations = []

        def cleanup(path, *args, **kwargs):
            if path == self.retained_scratch:
                cleanup_observations.append(any((store_path / "blobs").rglob("*")))
            return original_cleanup(path, *args, **kwargs)

        with patch(
            "literate_ai.cli.source_to_specification.shutil.rmtree", side_effect=cleanup
        ):
            status, document = self.invoke("--retained-evidence-store", str(store_path))
        self.assertEqual(status, 0, document)
        self.assertEqual(cleanup_observations, [True])
        retained = document["result"]["retained_evidence"]
        reference = BlobRef.from_dict(retained["archive"])
        store = FileSystemEvidenceStore(store_path)
        reader = reopen_qualification_archive(
            store.get_bytes(reference),
            reference,
            max_bytes=256 * 1024 * 1024,
            max_records=100_000,
        )
        self.assertEqual(
            reader.read_json(
                ContentIdentity.parse_uri(retained["qualification_identity"])
            ),
            {"fixture": "accepted-two-run-result"},
        )
        self.assertEqual(len(retained["runs"]), 2)
        self.assertEqual(
            json.loads(self.output.read_text())["retained_evidence"], retained
        )
        self.assertFalse(self.retained_scratch.exists())
        self.authority_store.append_qualified.assert_called_once()

    def test_retention_write_failure_prevents_qualification_admission(self):
        from literate_ai.security.evidence.storage import EvidenceStorageError

        self.qualify.side_effect = self.retained_runs
        with patch(
            "literate_ai.adapters.evidence_storage.FileSystemEvidenceStore.put_bytes",
            side_effect=EvidenceStorageError("evidence.storage.write-failed"),
        ):
            status, document = self.invoke(
                "--retained-evidence-store", str(self.fixture.root / "evidence")
            )
        self.assertNotEqual(status, 0, document)
        self.assertEqual(
            document["error"]["code"], "qualification.retained_publication_failed"
        )
        self.authority_store.append_qualified.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.retained_scratch.exists())

    def test_authority_drift_after_publication_prevents_admission(self):
        from literate_ai.adapters.evidence_storage import FileSystemEvidenceStore

        original_put = FileSystemEvidenceStore.put_bytes
        self.qualify.side_effect = self.retained_runs
        store_path = self.fixture.root / "evidence"

        def publish_then_change(store, content, *, media_type):
            reference = original_put(store, content, media_type=media_type)
            (
                self.project.root / "verification" / "acceptance" / "harness.py"
            ).write_bytes(b"changed")
            return reference

        with patch.object(
            FileSystemEvidenceStore, "put_bytes", new=publish_then_change
        ):
            status, document = self.invoke("--retained-evidence-store", str(store_path))
        self.assertNotEqual(status, 0, document)
        self.authority_store.append_qualified.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.retained_scratch.exists())
        # Immutable unreferenced bytes are retained; failure does not remove an
        # object that another transaction could already reference.
        self.assertTrue(
            any(path.is_file() for path in (store_path / "blobs").rglob("*"))
        )

    def test_retention_readback_failure_prevents_admission(self):
        from literate_ai.adapters.evidence_storage import FileSystemEvidenceStore

        self.qualify.side_effect = self.retained_runs
        with patch.object(
            FileSystemEvidenceStore, "get_bytes", return_value=b"substituted"
        ):
            status, document = self.invoke(
                "--retained-evidence-store", str(self.fixture.root / "evidence")
            )
        self.assertNotEqual(status, 0, document)
        self.assertEqual(
            document["error"]["code"], "qualification.retained_publication_failed"
        )
        self.authority_store.append_qualified.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.retained_scratch.exists())

    def test_retention_inside_baseline_refuses_without_writing(self):
        store_path = self.source / "evidence"
        before = inventory_source(self.source).identity
        status, document = self.invoke("--retained-evidence-store", str(store_path))
        self.assertNotEqual(status, 0, document)
        self.assertEqual(
            document["error"]["code"], "qualification.evidence_store_in_source"
        )
        self.qualify.assert_not_called()
        self.assertFalse(store_path.exists())
        self.assertEqual(inventory_source(self.source).identity, before)

    def test_retention_inside_scratch_refuses_before_lifecycle(self):
        scratch = self.fixture.root / "explicit-scratch"
        scratch.mkdir()
        foreign = scratch / "keep.txt"
        foreign.write_text("keep")
        status, document = self.invoke(
            "--scratch-root",
            str(scratch),
            "--retained-evidence-store",
            str(scratch / "evidence"),
        )
        self.assertNotEqual(status, 0, document)
        self.assertEqual(
            document["error"]["code"], "qualification.evidence_store_in_scratch"
        )
        self.qualify.assert_not_called()
        self.assertEqual(foreign.read_text(), "keep")
        self.authority_store.append_qualified.assert_not_called()

    def test_public_qualification_uses_library_oracle_and_projects_accepted_result(
        self,
    ):
        status, document = self.invoke()
        self.assertEqual(status, 0, document)
        self.assertTrue(document["result"]["qualified"])
        self.assertEqual(
            document["result"]["release_implementation_authority"], "specification"
        )
        self.assertTrue(self.output.is_file())
        self.qualify.assert_called_once()
        self.authority_store.append_qualified.assert_called_once()

    def test_explicit_model_binds_public_plan_and_clean_lifecycle(self):
        selected = "fixture/selected-model"
        with patch(
            "literate_ai.cli.generation.plan_from_args", return_value=self.plan
        ) as planning:
            status, document = self.invoke("--model", selected)
        self.assertEqual(status, 0, document)
        self.assertEqual(planning.call_args.args[0].model, selected)
        adapter = self.qualify.call_args.args[0]
        self.assertEqual(adapter.lifecycle.pipeline_model, selected)
        self.assertEqual(len(self.qualify.call_args.args[1].run_identities), 2)
        self.authority_store.append_qualified.assert_called_once()

    def test_public_qualification_rejects_type_drift_before_lifecycle_or_admission(
        self,
    ):
        for case in (
            LocalQualificationCase("known", ({"nested": [1]},), {"value": True}),
            LocalQualificationCase("known", ({"nested": [True]},), {"value": 1}),
        ):
            with self.subTest(case=case):
                self.write_profile(replace(self.profile, cases=(case,)))
                status, document = self.invoke()
                self.assertNotEqual(status, 0, document)
                self.assertEqual(
                    document["error"]["code"],
                    "qualification.library_oracle_profile_mismatch",
                )
                self.assertFalse(self.output.exists())
                self.assertFalse((self.project.root / "provenance").exists())
                self.qualify.assert_not_called()
                self.authority_store.append_qualified.assert_not_called()

    def assert_final_authority_failure(self, mutate, code):
        def complete(adapter, plan):
            result = self.accepted_runs(adapter, plan)
            mutate()
            return result

        self.qualify.side_effect = complete
        status, document = self.invoke()
        self.assertNotEqual(status, 0, document)
        self.assertEqual(document["error"]["code"], code)
        self.qualify.assert_called_once()
        self.authority_store.append_qualified.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertFalse((self.project.root / "provenance").exists())

    def test_final_admission_reopens_promotion_evidence(self):
        from literate_ai.source_to_specification.promotion_materialization import (
            SourcePromotionError,
        )

        with patch(
            "literate_ai.application.SourcePromotionService.verify_evidence",
            side_effect=[
                self.verified_promotion,
                SourcePromotionError("promotion.evidence_mismatch", "changed audit"),
            ],
        ) as verifier:
            self.assert_final_authority_failure(
                lambda: None, "qualification.promotion_evidence_invalid"
            )
        self.assertEqual(verifier.call_count, 2)

    def test_final_admission_reopens_harness_after_accepted_runs(self):
        harness = self.project.root / "verification" / "acceptance" / "harness.py"
        self.assert_final_authority_failure(
            lambda: harness.write_bytes(b"changed during qualification\n"),
            "component_acceptance.harness_changed",
        )

    def test_final_admission_reopens_oracle_authority_with_unchanged_cases(self):
        path = self.project.root / "verification" / "acceptance" / "library.json"

        def mutate():
            oracle = json.loads(path.read_text())
            oracle["public_interface_identities"] = [identity("foreign-interface").uri]
            path.write_text(json.dumps(oracle))

        self.assert_final_authority_failure(
            mutate, "qualification.acceptance_oracle_changed"
        )

    def test_final_admission_reobserves_installed_distribution(self):
        observed = [self.fixture.binding.distribution]
        binding = replace(
            self.fixture.binding, _distribution_observer=lambda: observed[0]
        )

        def mutate():
            observed[0] = replace(observed[0], distribution_version="0.2.1")

        with patch(
            "literate_ai.adapters.standard_lifecycle_binding.resolve_standard_project_lifecycle_driver",
            return_value=binding,
        ):
            self.assert_final_authority_failure(mutate, "standard_binding.changed")

    def test_public_qualification_still_rejects_a_changed_verifier_harness(self):
        (self.project.root / "verification" / "acceptance" / "harness.py").write_bytes(
            b"changed\n"
        )
        status, document = self.invoke()
        self.assertNotEqual(status, 0, document)
        self.assertEqual(
            document["error"]["code"], "component_acceptance.harness_changed"
        )
        self.qualify.assert_not_called()
        self.authority_store.append_qualified.assert_not_called()


if __name__ == "__main__":
    unittest.main()
