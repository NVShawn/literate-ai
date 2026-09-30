"""Native package layout integrity; these fixtures grant no qualification or trust."""

import hashlib
import unittest
from dataclasses import replace

from literate_ai.adapters.directory_artifacts import (
    DirectoryExportFile,
    encode_directory_export,
)
from literate_ai.adapters.qualification_capture import QualificationRunCapture
from literate_ai.adapters.retained_cargo_plan import (
    reopen_retained_cargo_plan,
    verify_retained_cargo_package_manifests,
)
from literate_ai.application.artifact_graph import create_artifact_build_graph
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.cargo_workspace import (
    CargoPackageExpectation,
    CargoTargetExpectation,
)
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.library_products import LibraryArtifactProduct
from literate_ai.contracts.retained_cargo import CargoManifestChange
from literate_ai.contracts.retained_libraries import RetainedLibraryExportSet
from tests.unit import test_artifact_graph_contracts as graphs
from tests.unit import test_library_products as libraries
from tests.unit import test_qualification_capture as captures
from tests.unit import test_retained_cargo_plan as plans

MANIFEST = (
    b'[package]\nname = "import-proof"\nversion = "1.0.0"\nedition = "2021"\n'
    b'[lib]\nname = "import_proof"\npath = "src/lib.rs"\n'
)


def blob(content, media="application/json"):
    return BlobRef(hashlib.sha256(content).hexdigest(), len(content), media_type=media)


class RetainedCargoPackageManifestTests(unittest.TestCase):
    def fixture(self, *, manifest=MANIFEST, path="source/Cargo.toml"):
        payload = encode_directory_export(
            (
                DirectoryExportFile(path, manifest, 0o644),
                DirectoryExportFile(
                    "source/src/lib.rs",
                    b"pub fn checked_sum(a:i64,b:i64)->Option<i64>{a.checked_add(b)}\n",
                    0o644,
                ),
            ),
            max_bytes=10000,
            max_entries=10,
        )
        reference = blob(payload, "application/zip")
        source = libraries.library_product("rust")
        export = replace(
            source.artifact_export,
            blob=reference,
            media_type=reference.media_type,
            dependency_artifact_identities=(),
        )
        surface = replace(source.import_surface, package="import_proof")
        builder = graphs.ArtifactGraphTests()
        graph = create_artifact_build_graph(
            build_system_driver_identity=builder.driver,
            manifests=(builder.manifest(export),),
            link_roots=(export.identity,),
        )
        exports = RetainedLibraryExportSet(
            graph,
            graph.link_plans[0].identity,
            (LibraryArtifactProduct(export, surface),),
        )
        plan, binding = plans.RetainedCargoPlanTests().fixture()
        package = CargoPackageExpectation(
            "_build/libs/p0/source",
            "import-proof",
            "1.0.0",
            (),
            (
                CargoTargetExpectation(
                    "import_proof",
                    ("lib",),
                    ("lib",),
                    "_build/libs/p0/source/src/lib.rs",
                    "2021",
                    True,
                    True,
                ),
            ),
        )
        plan = replace(
            plan,
            graph=replace(
                plan.graph,
                packages=(package,),
                members=(package.root,),
                default_members=(package.root,),
            ),
            manifests=(
                CargoManifestChange("Cargo.lock", None, blob(b"lock")),
                CargoManifestChange("Cargo.toml", None, blob(b"workspace")),
                CargoManifestChange(package.root + "/Cargo.toml", None, blob(manifest)),
            ),
        )
        binding = replace(
            binding,
            exports=exports,
            destinations=((export.identity, "_build/libs/p0"),),
        )
        binding = self.bind(plan, binding)
        run = replace(captures.fixture()[0], run_identity=binding.run_identity)
        capture = QualificationRunCapture(run, exports, (), ((reference, payload),))
        return plan, binding, capture

    @staticmethod
    def bind(plan, binding):
        return replace(
            binding, workspace_plan=blob(canonical_json_bytes(plan.to_dict()))
        )

    def verify(self, plan, binding, capture, **kwargs):
        return verify_retained_cargo_package_manifests(
            plan,
            binding,
            capture,
            max_package_bytes=kwargs.get("max_package_bytes", 10000),
            max_entries=10,
        )

    def test_nested_manifest_and_distinct_cargo_package_import_names(self):
        plan, binding, capture = self.fixture()
        content = canonical_json_bytes(plan.to_dict())
        self.assertEqual(
            reopen_retained_cargo_plan(content, binding, max_bytes=10000), plan
        )
        self.verify(plan, binding, capture)
        plan, binding, capture = self.fixture(
            manifest=MANIFEST.replace(b'name = "import_proof"\n', b"")
        )
        self.verify(plan, binding, capture)

    def test_reviewed_graph_cannot_substitute_package_name_or_version(self):
        plan, binding, capture = self.fixture()
        for change in ({"name": "other-package"}, {"version": "2.0.0"}):
            changed = replace(
                plan,
                graph=replace(
                    plan.graph, packages=(replace(plan.graph.packages[0], **change),)
                ),
            )
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(ValueError, "package-manifest-mismatch"),
            ):
                self.verify(changed, self.bind(changed, binding), capture)

    def test_manifest_identity_import_name_and_workspace_inheritance_refuse(self):
        for manifest in (
            MANIFEST.replace(b'"import_proof"', b'"foreign"'),
            MANIFEST.replace(b'version = "1.0.0"', b"version.workspace = true"),
            MANIFEST.replace(b"[lib]", b'workspace = "../../old"\n[lib]'),
        ):
            with (
                self.subTest(manifest=manifest),
                self.assertRaisesRegex(ValueError, "package-manifest-mismatch"),
            ):
                self.verify(*self.fixture(manifest=manifest))
        plan, binding, capture = self.fixture()
        changed = replace(
            plan,
            manifests=(
                *plan.manifests[:2],
                replace(plan.manifests[2], after=blob(b"other")),
            ),
        )
        with self.assertRaisesRegex(ValueError, "package-manifest-mismatch"):
            self.verify(changed, self.bind(changed, binding), capture)
        with self.assertRaisesRegex(ValueError, "package-manifest-mismatch"):
            self.verify(*self.fixture(path="Cargo.toml"))

    def test_missing_or_oversized_package_refuses(self):
        plan, binding, capture = self.fixture()
        for selected, limit in ((replace(capture, blobs=()), 10000), (capture, 10)):
            with (
                self.subTest(limit=limit),
                self.assertRaisesRegex(ValueError, "package-manifest-mismatch"),
            ):
                self.verify(plan, binding, selected, max_package_bytes=limit)

    def test_ambiguous_nested_package_roots_refuse(self):
        plan, binding, capture = self.fixture()
        duplicate = replace(plan.graph.packages[0], root="_build/libs/p0/other")
        changed = replace(
            plan,
            graph=replace(plan.graph, packages=(*plan.graph.packages, duplicate)),
            manifests=tuple(
                sorted(
                    (
                        *plan.manifests,
                        CargoManifestChange(
                            duplicate.root + "/Cargo.toml", None, blob(MANIFEST)
                        ),
                    ),
                    key=lambda m: m.path,
                )
            ),
        )
        with self.assertRaisesRegex(ValueError, "bound-library-mismatch"):
            self.verify(changed, self.bind(changed, binding), capture)
