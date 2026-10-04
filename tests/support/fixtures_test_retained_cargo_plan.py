"""Shared fixtures extracted from ``tests.unit.test_retained_cargo_plan``."""

import copy

import hashlib

import unittest

from dataclasses import replace

from literate_ai.adapters.retained_cargo_plan import (
    reopen_retained_cargo_plan,
    verify_retained_cargo_inputs,
)

from literate_ai.application.artifact_graph import create_artifact_build_graph

from literate_ai.application.source_promotion import qualify_locked_source_promotion

from literate_ai.contracts.blobs import BlobRef

from literate_ai.contracts.cargo_workspace import (
    CargoPackageExpectation,
    CargoTargetExpectation,
    CargoWorkspaceExpectation,
)

from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes

from literate_ai.contracts.repositories import RepositoryBuildCommand

from literate_ai.contracts.retained_cargo import (
    CargoManifestChange,
    RetainedCargoWorkspacePlan,
)

from literate_ai.contracts.retained_libraries import RetainedLibraryExportSet

from tests.support import fixtures_test_artifact_graph_contracts as graphs

from tests.support import fixtures_test_locked_source_promotion as promotions

from tests.support import fixtures_test_retained_library_binding as bindings

from tests.support import fixtures_test_schema_catalog as schemas

class RetainedCargoPlanTests(unittest.TestCase):
    def importer_fixture(self):
        provider = promotions.LockedSourcePromotionTests()
        provider.setUp()
        self.addCleanup(provider.doCleanups)
        qualified = qualify_locked_source_promotion(
            provider.authority, provider.evidence
        )
        promotion = replace(provider.evidence, authority_projection=qualified)
        plan, binding = self.fixture()
        root = next(
            p
            for p in binding.exports.libraries
            if p.artifact_export.identity
            in binding.exports.link_plan.resolved_root_artifact_identities
        )
        destination = dict(binding.destinations)[root.artifact_export.identity]
        export = replace(
            root.artifact_export,
            component_revision=provider.authority.lock.root_revision,
            dependency_artifact_identities=(),
        )
        builder = graphs.ArtifactGraphTests()
        graph = create_artifact_build_graph(
            build_system_driver_identity=builder.driver,
            manifests=(builder.manifest(export),),
            link_roots=(export.identity,),
        )
        exports = RetainedLibraryExportSet(
            graph,
            graph.link_plans[0].identity,
            (replace(root, artifact_export=export),),
        )
        binding = replace(
            binding,
            exports=exports,
            destinations=((export.identity, destination),),
            qualification_identity=provider.qualification.identity,
            run_identity=provider.qualification.runs[0].run_identity,
            verifier_identity=provider.evidence.verifier_identity,
            policy_identity=provider.evidence.policy_identity,
        )
        arguments = dict(
            importer_project_id=binding.importer_project_id,
            reviewed_binding_identity=binding.identity,
            configured_store_id=binding.source_store_id,
            current_generation_closure=provider.closure,
            current_verifier_identity=provider.evidence.verifier_identity,
            current_policy_identity=provider.evidence.policy_identity,
            current_cargo_identity=plan.cargo_identity,
            current_rustc_identity=plan.rustc_identity,
            current_gates=plan.gates,
            max_bytes=100000,
        )
        return plan, binding, provider.authority, promotion, arguments

    def test_current_importer_provider_tools_and_gate_authority_are_required(self):
        plan, binding, authority, promotion, arguments = self.importer_fixture()
        content = canonical_json_bytes(plan.to_dict())
        self.assertEqual(
            verify_retained_cargo_inputs(
                content, binding, authority, promotion, **arguments
            ),
            plan,
        )
        for changes in (
            {"importer_project_id": "foreign"},
            {"reviewed_binding_identity": canonical_identity("other-review")},
            {"configured_store_id": "other-store"},
            {"current_cargo_identity": canonical_identity("other-cargo")},
            {"current_rustc_identity": canonical_identity("other-rustc")},
            {"current_gates": ()},
            {
                "current_gates": (
                    RepositoryBuildCommand("full-test", ("make", "partial-test")),
                )
            },
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                verify_retained_cargo_inputs(
                    content, binding, authority, promotion, **{**arguments, **changes}
                )

    def test_reviewed_binding_still_cannot_substitute_provider_qualification(self):
        plan, binding, authority, promotion, arguments = self.importer_fixture()
        for changes in (
            {"qualification_identity": canonical_identity("foreign-qualification")},
            {"run_identity": canonical_identity("foreign-run")},
            {"verifier_identity": canonical_identity("foreign-verifier")},
            {"policy_identity": canonical_identity("foreign-policy")},
        ):
            changed = replace(binding, **changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                verify_retained_cargo_inputs(
                    canonical_json_bytes(plan.to_dict()),
                    changed,
                    authority,
                    promotion,
                    **{**arguments, "reviewed_binding_identity": changed.identity},
                )

    def fixture(self):
        binding = bindings.RetainedLibraryBindingTests().fixture()
        destinations = dict(binding.destinations)
        packages = tuple(
            CargoPackageExpectation(
                destinations[p.artifact_export.identity],
                p.import_surface.package,
                "1.0.0",
                (),
                (
                    CargoTargetExpectation(
                        p.import_surface.package,
                        ("lib",),
                        ("lib",),
                        destinations[p.artifact_export.identity] + "/src/lib.rs",
                        "2021",
                        True,
                        True,
                    ),
                ),
            )
            for p in binding.exports.libraries
        )
        graph = CargoWorkspaceExpectation(
            packages,
            (),
            tuple(p.root for p in packages),
            tuple(p.root for p in packages),
            None,
            "cargo-output",
        )
        reference = BlobRef("a" * 64, 10)
        manifests = tuple(
            CargoManifestChange(path, reference, reference)
            for path in sorted(
                {"Cargo.toml", "Cargo.lock"}
                | {p.root + "/Cargo.toml" for p in packages}
            )
        )
        plan = RetainedCargoWorkspacePlan(
            ".",
            graph,
            manifests,
            canonical_identity("cargo"),
            canonical_identity("rustc"),
            "x86_64-unknown-linux-gnu",
            (),
            False,
            False,
            (RepositoryBuildCommand("full-test", ("make", "test")),),
        )
        return plan, self.bind(plan, binding)

    def bind(self, plan, binding):
        content = canonical_json_bytes(plan.to_dict())
        return replace(
            binding,
            workspace_plan=BlobRef(
                hashlib.sha256(content).hexdigest(),
                len(content),
                media_type="application/json",
            ),
        )

    def test_roundtrip_schema_and_exact_binding_reopen(self):
        plan, binding = self.fixture()
        self.assertEqual(RetainedCargoWorkspacePlan.from_dict(plan.to_dict()), plan)
        schemas.SchemaCatalog().validate(plan.SCHEMA, plan.to_dict())
        content = canonical_json_bytes(plan.to_dict())
        self.assertEqual(
            reopen_retained_cargo_plan(content, binding, max_bytes=len(content)), plan
        )
        self.assertEqual(
            plan.metadata_command(offline=True).argv,
            (
                "cargo",
                "metadata",
                "--locked",
                "--format-version=1",
                "--filter-platform",
                "x86_64-unknown-linux-gnu",
                "--offline",
            ),
        )

    def test_graph_fields_and_manifest_coverage_cannot_be_omitted(self):
        plan, _ = self.fixture()
        for path in (
            "Cargo.lock",
            "Cargo.toml",
            plan.graph.packages[0].root + "/Cargo.toml",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                replace(
                    plan, manifests=tuple(m for m in plan.manifests if m.path != path)
                )
        for mutation in (
            lambda d: d["graph"].update(accepted=True),
            lambda d: d["graph"]["packages"][0]["targets"][0].pop("test"),
            lambda d: d["manifests"][0].update(trusted=True),
        ):
            data = copy.deepcopy(plan.to_dict())
            mutation(data)
            with self.assertRaises(ValueError):
                RetainedCargoWorkspacePlan.from_dict(data)

    def test_unsafe_output_paths_and_duplicate_gate_names_refuse(self):
        plan, _ = self.fixture()
        for changes in (
            {"workspace_root": "../outside"},
            {"target": "--bad"},
            {"target": "target.json"},
            {"features": ("a,b",)},
            {"gates": ()},
            {"gates": plan.gates + plan.gates},
            {"manifests": plan.manifests[::-1]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(plan, **changes)
        with self.assertRaises(ValueError):
            CargoManifestChange("CON/Cargo.toml", None, plan.manifests[0].after)

    def test_substitution_bounds_and_noncanonical_json_refuse(self):
        plan, binding = self.fixture()
        content = canonical_json_bytes(plan.to_dict())
        for candidate, maximum in (
            (content + b" ", len(content) + 1),
            (content, len(content) - 1),
            (content, True),
        ):
            with self.assertRaises(ValueError):
                reopen_retained_cargo_plan(candidate, binding, max_bytes=maximum)
        for candidate in (content + b" ", b'{"schema":"wrong",' + content[1:]):
            pinned = replace(
                binding,
                workspace_plan=BlobRef(
                    hashlib.sha256(candidate).hexdigest(),
                    len(candidate),
                    media_type="application/json",
                ),
            )
            with self.assertRaisesRegex(ValueError, "plan-invalid"):
                reopen_retained_cargo_plan(candidate, pinned, max_bytes=len(candidate))

    def test_rehashed_foreign_library_target_and_output_overlap_refuse(self):
        plan, binding = self.fixture()
        graph = replace(
            plan.graph,
            packages=(
                replace(
                    plan.graph.packages[0],
                    targets=(
                        replace(plan.graph.packages[0].targets[0], name="foreign"),
                    ),
                ),
            )
            + plan.graph.packages[1:],
        )
        changed = replace(plan, graph=graph)
        with self.assertRaisesRegex(ValueError, "bound-library-mismatch"):
            reopen_retained_cargo_plan(
                canonical_json_bytes(changed.to_dict()),
                self.bind(changed, binding),
                max_bytes=100000,
            )
        native = next(
            (identity, path)
            for identity, path in binding.destinations
            if identity
            not in {p.artifact_export.identity for p in binding.exports.libraries}
        )
        changed = replace(plan, graph=replace(plan.graph, output_directory=native[1]))
        with self.assertRaisesRegex(ValueError, "output-package-overlap"):
            reopen_retained_cargo_plan(
                canonical_json_bytes(changed.to_dict()),
                self.bind(changed, binding),
                max_bytes=100000,
            )

    def test_features_tools_and_existing_gate_commands_change_plan_identity(self):
        plan, _ = self.fixture()
        for changes in (
            {"features": ("client/extra",)},
            {"all_features": True},
            {"no_default_features": True},
            {"cargo_identity": canonical_identity("other")},
            {"gates": (RepositoryBuildCommand("full-test", ("make", "all-tests")),)},
        ):
            changed = replace(plan, **changes)
            self.assertNotEqual(changed.identity, plan.identity)
        command = replace(
            plan, features=("client/extra",), no_default_features=True
        ).metadata_command(offline=True)
        self.assertIn("--features=client/extra", command.argv)
        self.assertIn("--no-default-features", command.argv)

if __name__ == "__main__":
    unittest.main()

