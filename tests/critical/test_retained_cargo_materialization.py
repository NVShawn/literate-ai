"""Real archive-to-package transactions with foreign-state preservation."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import retained_cargo_materialization as materialization
from literate_ai.adapters.qualification_archive import encode_qualification_archive
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.adapters.retained_cargo_files import read_retained_cargo_files
from literate_ai.application.source_promotion import qualify_locked_source_promotion
from literate_ai.authority import ComponentAuthorityLifecycle
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.projects import serialize_project_configuration
from tests.support import fixtures_test_project_configuration as projects
from tests.support import fixtures_test_retained_cargo_import as imports
from tests.support.fixtures_test_retained_cargo_files import blob


class RetainedCargoMaterializationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = imports.RetainedCargoImportTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        temporary = tempfile.TemporaryDirectory(prefix="cm-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.binding_bytes = canonical_json_bytes(self.fixture.binding.to_dict())
        (self.root / "literate.project.json").write_bytes(
            serialize_project_configuration(
                replace(
                    projects._definition(),
                    project_id=self.fixture.binding.importer_project_id,
                )
            )
        )
        for name, content in (
            ("binding.json", self.binding_bytes),
            ("plan.json", self.fixture.plan_bytes),
            ("Cargo.toml", b"workspace"),
            ("Cargo.lock", b"lock"),
        ):
            (self.root / name).write_bytes(content)
        self.destinations = sorted(
            self.root / path for _, path in self.fixture.binding.destinations
        )

    def snapshot(self):
        return read_retained_cargo_files(
            self.root,
            binding_path="binding.json",
            reviewed_binding=blob(self.binding_bytes),
            plan_path="plan.json",
            manifest_state="after",
            package_state="provision",
        )

    def run_materializer(self, **changes):
        return materialization.materialize_retained_cargo_archive(
            self.snapshot(),
            environment={"OBJ_DIR": "pkgs"},
            **{**self.fixture.calls, **changes},
        )

    def test_publication_failure_rolls_back_only_created_packages(self):
        original = materialization.publish_directory_exclusive
        count = 0

        def fail_second(source, destination, **kwargs):
            nonlocal count
            if destination in self.destinations:
                count += 1
                if count == 2:
                    destination.mkdir()
                    (destination / "foreign").write_bytes(b"keep")
                    raise FileExistsError("concurrent foreign destination")
            return original(source, destination, **kwargs)

        with patch.object(
            materialization, "publish_directory_exclusive", side_effect=fail_second
        ):
            with self.assertRaises(OrchestrationInventoryError) as raised:
                self.run_materializer()
        self.assertEqual(
            raised.exception.code,
            "orchestration.retained_materialization_publication_failed",
        )
        self.assertFalse(self.destinations[0].exists())
        self.assertEqual((self.destinations[1] / "foreign").read_bytes(), b"keep")
        self.assertTrue(list((self.root / "pkgs").glob(".rp-*")))

    def test_fresh_recertification_archive_materializes_reviewed_rust_packages(self):
        result, captures, entries = self.fixture.capture_fixture.typed_product_fixture(
            promotion_audit=self.fixture.promotion_audit,
            artifact_payloads=self.fixture.artifact_payloads,
            allocation_salt="recertification",
        )
        archive = encode_qualification_archive(
            entries, max_bytes=5_000_000, max_records=1000
        )
        old_promotion = self.fixture.inputs.promotion
        invalidated = ComponentAuthorityLifecycle.invalidate(
            old_promotion.authority_projection,
            evidence_identities=(result.identity,),
        )
        closure = replace(
            self.fixture.inputs.current_generation_closure,
            qualification_evidence_identity=result.identity,
        )
        fresh_promotion = replace(
            old_promotion,
            authority_projection=invalidated,
            generation_closure=closure,
            verifier_identity=result.case_map.verifier_identity,
            policy_identity=result.runs[0].lifecycle_policy_identity,
            qualification_lifecycle_result=result,
        )
        fresh_promotion = replace(
            fresh_promotion,
            authority_projection=qualify_locked_source_promotion(
                self.fixture.inputs.authority, fresh_promotion
            ),
        )
        binding = replace(
            self.fixture.binding,
            exports=captures[0].exports,
            qualification_archive=imports.blob(archive, "application/zip"),
            qualification_identity=result.identity,
            run_identity=captures[0].run.run_identity,
            verifier_identity=result.case_map.verifier_identity,
            policy_identity=result.runs[0].lifecycle_policy_identity,
        )
        self.fixture.archive = archive
        self.fixture.binding = binding
        self.fixture.inputs = replace(
            self.fixture.inputs,
            promotion=fresh_promotion,
            reviewed_binding_identity=binding.identity,
            current_generation_closure=closure,
            current_verifier_identity=result.case_map.verifier_identity,
            current_policy_identity=result.runs[0].lifecycle_policy_identity,
        )
        self.binding_bytes = canonical_json_bytes(binding.to_dict())
        (self.root / "binding.json").write_bytes(self.binding_bytes)

        materialized = self.run_materializer()
        self.assertEqual(len(materialized.packages), len(self.destinations))
        self.assertTrue(all(path.is_dir() for path in self.destinations))
