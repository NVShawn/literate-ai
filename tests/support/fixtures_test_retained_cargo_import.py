"""Shared fixtures extracted from ``tests.unit.test_retained_cargo_import``."""

import hashlib

import tempfile

import unittest

from dataclasses import replace

from pathlib import Path

from literate_ai.adapters.directory_artifacts import (
    DirectoryExportFile,
    encode_directory_export,
)

from literate_ai.adapters.qualification_archive import encode_qualification_archive

from literate_ai.adapters.retained_cargo_import import (
    RetainedCargoImportInputs,
    verify_retained_cargo_archive,
)

from literate_ai.application.locked_generation_authority import (
    LockedGenerationAuthority,
)

from literate_ai.application.source_promotion import qualify_locked_source_promotion

from literate_ai.authority import ComponentAuthorityLifecycle

from literate_ai.contracts.authority import ComponentGenerationClosure

from literate_ai.contracts.blobs import BlobRef

from literate_ai.contracts.cargo_workspace import (
    CargoPackageExpectation,
    CargoTargetExpectation,
    CargoWorkspaceExpectation,
)

from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)

from literate_ai.contracts.repositories import RepositoryBuildCommand

from literate_ai.contracts.retained_cargo import (
    CargoManifestChange,
    RetainedCargoWorkspacePlan,
)

from literate_ai.contracts.retained_libraries import RetainedLibraryBinding

from literate_ai.source_to_specification.promotion_materialization import (
    PromotionInputKind,
    SourcePromotionInput,
    SourcePromotionMaterializer,
    VerifiedSourcePromotionEvidence,
)

from tests.support import fixtures_test_component_execution_planning as execution_fixtures

from tests.support import fixtures_test_component_lock_contracts as locks

from tests.support import fixtures_test_qualification_capture as captures

from tests.support import fixtures_test_standard_project_lifecycle as lifecycle_fixtures

def blob(content, media="application/json"):
    return BlobRef(hashlib.sha256(content).hexdigest(), len(content), media_type=media)

class RetainedCargoImportTests(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = b"authored promotion input"
            (root / "component.md").write_bytes(content)
            audit = SourcePromotionMaterializer().audit(
                (
                    SourcePromotionInput(
                        PromotionInputKind.COMPONENT_INTENT,
                        root,
                        "fixture",
                        "component.md",
                        "component.md",
                        blob(content).identity,
                    ),
                )
            )
        fixture = captures.QualificationCaptureTests()
        producer = lifecycle_fixtures.StandardProjectLifecycleTests()
        producer.setUp()
        self.native_manifests = {
            name: (
                f'[package]\nname = "{name}"\nversion = "1.0.0"\n'
                '[lib]\nname = "fixture"\npath = "src/lib.rs"\n'
            ).encode()
            for name in producer.names.values()
        }
        payloads = {
            name: encode_directory_export(
                (
                    DirectoryExportFile("source/Cargo.toml", manifest, 0o644),
                    DirectoryExportFile(
                        "source/src/lib.rs", b"pub fn run() {}\n", 0o644
                    ),
                ),
                max_bytes=4096,
                max_entries=10,
            )
            for name, manifest in self.native_manifests.items()
        }
        result, products, entries = fixture.typed_product_fixture(
            promotion_audit=audit, artifact_payloads=payloads
        )
        self.entries = entries
        self.products = products[0]
        self.archive = encode_qualification_archive(
            entries, max_bytes=5_000_000, max_records=1000
        )
        lock = fixture.current_lock
        authoring = locks.component_authoring(
            "invoice-cli",
            requirements=(
                execution_fixtures._requirement("pricing", "pricing-api"),
                execution_fixtures._requirement("reporting", "reporting-api"),
            ),
        )
        authority = LockedGenerationAuthority(lock, authoring, (authoring,), (), ())
        node = next(n for n in lock.nodes if n.revision.identity == lock.root_revision)
        inventory = ComponentAuthorityLifecycle.inventory(
            component_coordinate=authoring.coordinate.uri,
            source_snapshot_identity=result.runs[0].source_snapshot_identity,
            provenance_reference_identity=canonical_identity("promotion"),
            evidence_identities=(canonical_identity("inventory"),),
        )
        derived = ComponentAuthorityLifecycle.derive(
            inventory,
            component_revision_identity=lock.root_revision,
            specification_set_identity=node.revision.specification_set_identity,
            evidence_identities=(canonical_identity("derivation"),),
        )
        retained = ComponentAuthorityLifecycle.accept(
            derived, evidence_identities=(canonical_identity("review"),)
        )
        closure = ComponentGenerationClosure(
            *(
                canonical_identity(name)
                for name in ("flavors", "skills", "workflow", "routing")
            ),
            ContentIdentity.parse_uri(audit.identity),
            ContentIdentity.parse_uri(audit.materialized_tree_identity),
            result.identity,
        )
        promotion = VerifiedSourcePromotionEvidence(
            (audit,),
            authority_projection=retained,
            target_lock_identity=lock.identity,
            generation_closure=closure,
            verifier_identity=result.case_map.verifier_identity,
            policy_identity=result.runs[0].lifecycle_policy_identity,
            qualification_lifecycle_result=result,
        )
        promotion = replace(
            promotion,
            authority_projection=qualify_locked_source_promotion(authority, promotion),
        )
        destinations = tuple(
            (e.identity, f"pkgs/p{i}")
            for i, e in enumerate(
                sorted(
                    (
                        e
                        for m in self.products.exports.graph.manifests
                        for e in m.exports
                    ),
                    key=lambda e: e.identity.uri,
                )
            )
        )
        paths = dict(destinations)
        packages = tuple(
            CargoPackageExpectation(
                paths[p.artifact_export.identity] + "/source",
                p.artifact_export.export_id.removeprefix("artifact-"),
                "1.0.0",
                (),
                (
                    CargoTargetExpectation(
                        p.import_surface.package,
                        ("lib",),
                        ("lib",),
                        paths[p.artifact_export.identity] + "/source/src/lib.rs",
                        "2021",
                        True,
                        True,
                    ),
                ),
            )
            for p in self.products.exports.libraries
        )
        graph = CargoWorkspaceExpectation(
            packages,
            (),
            tuple(p.root for p in packages),
            tuple(p.root for p in packages),
            None,
            "cargo-output",
        )
        manifest_bytes = {
            "Cargo.toml": b"workspace",
            "Cargo.lock": b"lock",
            **{p.root + "/Cargo.toml": self.native_manifests[p.name] for p in packages},
        }
        manifests = tuple(
            CargoManifestChange(path, blob(b"before"), blob(content))
            for path, content in sorted(manifest_bytes.items())
        )
        gates = (RepositoryBuildCommand("existing-full-tests", ("make", "test")),)
        self.plan = RetainedCargoWorkspacePlan(
            ".",
            graph,
            manifests,
            canonical_identity("cargo"),
            canonical_identity("rustc"),
            "x86_64-unknown-linux-gnu",
            (),
            False,
            False,
            gates,
        )
        self.plan_bytes = canonical_json_bytes(self.plan.to_dict())
        self.binding = RetainedLibraryBinding(
            "importer",
            "reviewed-store",
            self.products.exports,
            blob(self.archive, "application/zip"),
            result.identity,
            self.products.run.run_identity,
            promotion.verifier_identity,
            promotion.policy_identity,
            blob(self.plan_bytes),
            destinations,
        )
        self.guards = 0

        def guard():
            self.guards += 1

        self.inputs = RetainedCargoImportInputs(
            authority,
            promotion,
            "importer",
            self.binding.identity,
            "reviewed-store",
            closure,
            promotion.verifier_identity,
            promotion.policy_identity,
            self.plan.cargo_identity,
            self.plan.rustc_identity,
            gates,
            fixture.current_oracle,
            fixture.current_recipes,
            fixture.current_commands,
            fixture.current_profile,
            fixture.current_driver,
            guard,
        )
        self.reads = []
        self.calls = dict(
            read_current=lambda: self.inputs,
            read_archive=self.read_archive,
            max_plan_bytes=100000,
            max_archive_bytes=5_000_000,
            max_records=1000,
            max_package_bytes=4096,
        )

    def read_archive(self, store, reference, maximum):
        self.reads.append((store, reference, maximum))
        return self.archive

    def run_import(self, **changes):
        return verify_retained_cargo_archive(
            self.binding, self.plan_bytes, **{**self.calls, **changes}
        )

    def test_complete_archive_reopens_before_any_product_is_returned(self):
        self.assertTrue(self.inputs.current_commands)
        self.assertTrue(
            all(c.is_library for c in self.inputs.current_commands.values())
        )
        plan, capture = self.run_import()
        self.assertEqual((plan, capture), (self.plan, self.products))
        self.assertEqual(
            self.reads,
            [("reviewed-store", self.binding.qualification_archive, 5_000_000)],
        )
        self.assertEqual(self.guards, 4)

    def test_invalid_review_and_oversized_reference_refuse_before_transport(self):
        with self.assertRaises(ValueError):
            self.run_import(
                read_current=lambda: replace(
                    self.inputs, reviewed_binding_identity=canonical_identity("other")
                )
            )
        with self.assertRaises(ValueError):
            self.run_import(max_archive_bytes=len(self.archive) - 1)
        self.assertEqual(self.reads, [])

    def test_qualified_archive_cannot_substitute_reviewed_native_manifest(self):
        package = self.plan.graph.packages[0]
        manifest_path = package.root + "/Cargo.toml"
        changes = (
            replace(
                self.plan,
                manifests=tuple(
                    replace(item, after=blob(b"different reviewed bytes"))
                    if item.path == manifest_path
                    else item
                    for item in self.plan.manifests
                ),
            ),
            replace(
                self.plan,
                graph=replace(
                    self.plan.graph,
                    packages=(replace(package, name="foreign-package"),)
                    + self.plan.graph.packages[1:],
                ),
            ),
            replace(
                self.plan,
                graph=replace(
                    self.plan.graph,
                    packages=(replace(package, version="2.0.0"),)
                    + self.plan.graph.packages[1:],
                ),
            ),
        )
        for plan in changes:
            content = canonical_json_bytes(plan.to_dict())
            binding = replace(self.binding, workspace_plan=blob(content))
            current = replace(self.inputs, reviewed_binding_identity=binding.identity)
            with (
                self.subTest(plan=plan),
                self.assertRaisesRegex(
                    ValueError, "retained.cargo.package-manifest-mismatch"
                ),
            ):
                verify_retained_cargo_archive(
                    binding,
                    content,
                    **{
                        **self.calls,
                        "read_current": lambda current=current: current,
                    },
                )

    def test_archive_substitution_and_missing_evidence_refuse(self):
        with self.assertRaises(ValueError):
            self.run_import(read_archive=lambda *_: self.archive[:-1])
        with self.assertRaises(ValueError):
            self.run_import(max_records=1)
        incomplete = encode_qualification_archive(
            tuple(
                (identity, content)
                for identity, content in self.entries
                if identity != self.binding.qualification_identity
            ),
            max_bytes=5_000_000,
            max_records=1000,
        )
        binding = replace(
            self.binding, qualification_archive=blob(incomplete, "application/zip")
        )
        current = replace(self.inputs, reviewed_binding_identity=binding.identity)
        with self.assertRaisesRegex(ValueError, "record-missing"):
            verify_retained_cargo_archive(
                binding,
                self.plan_bytes,
                **{
                    **self.calls,
                    "read_current": lambda: current,
                    "read_archive": lambda *_: incomplete,
                },
            )

    def test_mid_read_filesystem_drift_prevents_return(self):
        changed = False

        def guard():
            if changed:
                raise ValueError("fixture inputs changed")

        current = replace(self.inputs, require_unchanged=guard)

        def read(*_):
            nonlocal changed
            changed = True
            return self.archive

        with self.assertRaisesRegex(ValueError, "fixture inputs changed"):
            self.run_import(read_current=lambda: current, read_archive=read)

    def test_second_current_observation_cannot_change_policy_or_recipes(self):
        for changed in (
            replace(
                self.inputs, current_policy_identity=canonical_identity("other-policy")
            ),
            replace(self.inputs, current_recipes={}),
        ):
            observations = iter((self.inputs, changed))
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.run_import(
                    read_current=lambda observations=observations: next(observations)
                )

    def test_recipe_maps_are_snapshots_and_guard_is_mandatory(self):
        recipes = dict(self.inputs.current_recipes)
        current = replace(self.inputs, current_recipes=recipes)
        recipes.clear()
        self.assertEqual(current.current_recipes, self.inputs.current_recipes)
        with self.assertRaises(TypeError):
            replace(self.inputs, require_unchanged=None)

if __name__ == "__main__":
    unittest.main()

