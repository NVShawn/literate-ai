from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_package_release_contracts``."""

import hashlib
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from literate_ai.adapters.packaging import DirectoryPackageAdapter
from literate_ai.application.artifact_graph import (
    ArtifactAssemblyError,
    create_artifact_build_graph,
    create_package_plan,
    create_resource_package_input,
    realize_manifest,
)
from literate_ai.application.release_artifacts import (
    ReleaseArtifactAssemblyError,
    StandardReleaseDeclaration,
    create_standard_artifact_build_graph,
    create_standard_release_artifact_set,
    standard_release_evidence_identities,
    standard_release_evidence_manifest_bytes,
)
from literate_ai.contracts import (
    BlobRef,
    ComponentCoordinate,
    ComponentRevisionRef,
    CppLibraryLayout,
    SourceBundleClosure,
    SourceBundleFile,
)
from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.executable_components.packages import (
    PackagedFile,
    PackageEntrypoint,
    PackageKind,
    PackagePlan,
    PackageResult,
    ReleaseArtifactSet,
    RuntimeRequirement,
    RuntimeRequirementKind,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.publication import (
    FilesystemPublicationTarget,
    PublicationError,
    PublicationPolicy,
    PublicationService,
    ReleasePublicationError,
    StandardProjectReleaseService,
    create_publication_request_from_release,
)
from literate_ai.security import SecurityProfile
from literate_ai.storage import AppendOnlyEventStore, FileSystemCAS
from tests.support.fixtures_test_artifact_graph_contracts import (
    ArtifactGraphTests,
    SourceAssemblyTests,
)
from tests.support.fixtures_test_component_execution_planning import _diamond_lock
from tests.support.fixtures_test_component_generation_scheduling import (
    _decision,
    _names,
    _prepared_execution,
)
from tests.support.fixtures_test_standard_project_lifecycle import (
    LifecyclePorts,
    _prepared_nodes,
    _service,
    _source_record,
)


def _identity(label: str):
    return canonical_identity({"package-fixture": label})


def _blob(content: bytes, media_type: str = "application/json") -> BlobRef:
    return BlobRef(
        hashlib.sha256(content).hexdigest(), len(content), media_type=media_type
    )


def _source_bundle(
    name: str,
) -> tuple[SourceBundleClosure, dict[str, bytes]]:
    tree_identity, file_blob, file_bytes, root_blob, root_bytes = _source_record(name)
    closure = SourceBundleClosure(
        root_blob,
        tree_identity,
        (SourceBundleFile("main.txt", file_blob),),
    )
    return closure, {
        root_blob.identity: root_bytes,
        file_blob.identity: file_bytes,
    }


def _release_declaration(execution, plan: PackagePlan) -> StandardReleaseDeclaration:
    return StandardReleaseDeclaration(
        execution.identity,
        execution.component_lock_identity,
        execution.root_revision,
        plan.target_identity,
        plan.root_artifact_identity,
        plan.package_kind,
        plan.packager_identity,
        tuple(
            sorted(
                (item.source_identity.uri, item.path)
                for item in plan.inputs
                if item.kind.value == "artifact"
            )
        ),
        plan.entrypoints,
        tuple(item for item in plan.inputs if item.kind.value == "resource"),
        plan.runtime_requirements,
    )


class PackageReleaseContractTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = ArtifactGraphTests()
        fixture.setUp()
        self.manifests = fixture.diamond()
        self.root_export = self.manifests[0].exports[0]
        self.graph = create_artifact_build_graph(
            build_system_driver_identity=fixture.driver,
            manifests=self.manifests,
            link_roots=(self.root_export.identity,),
        )
        exports = {
            export.identity.uri: export
            for manifest in self.graph.manifests
            for export in manifest.exports
        }
        self.destinations = {
            uri: (
                "bin/app"
                if uri == self.root_export.identity.uri
                else f"lib/{item.export_id}.bin"
            )
            for uri, item in exports.items()
        }
        self.entrypoint = PackageEntrypoint(
            "app",
            "application",
            "bin/app",
            self.root_export.identity,
        )
        self.interpreter = RuntimeRequirement(
            "python-runtime",
            RuntimeRequirementKind.INTERPRETER,
            "Python 3.11+",
            _identity("python-runtime"),
            False,
        )

    def plan(
        self,
        kind: PackageKind = PackageKind.RUNTIME_BUNDLE,
        requirements: tuple[RuntimeRequirement, ...] | None = None,
    ) -> PackagePlan:
        return create_package_plan(
            self.graph,
            root_component_revision=self.root_export.component_revision,
            component_lock_identity=_identity("component-lock"),
            target_identity=self.root_export.target_identity,
            root_artifact_identity=self.root_export.identity,
            package_kind=kind,
            packager_identity=_identity("packager"),
            destinations=self.destinations,
            entrypoints=(self.entrypoint,),
            runtime_requirements=(self.interpreter,)
            if requirements is None
            else requirements,
        )

    def result(self, plan: PackagePlan) -> PackageResult:
        files = tuple(
            PackagedFile(
                item.path,
                item.role,
                item.kind,
                item.source_identity,
                item.target_identity,
                item.blob,
                item.path == "bin/app",
            )
            for item in plan.inputs
        )
        return PackageResult(
            package_plan_identity=plan.identity,
            root_component_revision=plan.root_component_revision,
            component_lock_identity=plan.component_lock_identity,
            target_identity=plan.target_identity,
            artifact_graph_identity=plan.artifact_graph_identity,
            package_kind=plan.package_kind,
            packager_identity=plan.packager_identity,
            files=files,
            artifacts=files,
            entrypoints=plan.entrypoints,
            runtime_requirements=plan.runtime_requirements,
            native_library_root=plan.native_library_root,
            native_library_layout=plan.native_library_layout,
        )

    def release(self, plan: PackagePlan, result: PackageResult) -> ReleaseArtifactSet:
        evidence = (_identity("acceptance-receipt"),)
        evidence_bytes = standard_release_evidence_manifest_bytes(evidence)
        source_bundle, _source_bytes = _source_bundle("release-root")
        return ReleaseArtifactSet(
            root_component_ref=ComponentRevisionRef(
                ComponentCoordinate("test", "release-root"),
                "1.0.0",
                plan.root_component_revision,
            ),
            root_component_revision=plan.root_component_revision,
            root_source_bundle=source_bundle,
            component_lock_identity=plan.component_lock_identity,
            target_identity=plan.target_identity,
            artifact_graph_identity=plan.artifact_graph_identity,
            accepted_workspace_identities=(_identity("accepted-workspace"),),
            release_declaration_identities=(_identity("release-declaration"),),
            packages=(result,),
            resource_identities=(),
            evidence_identities=evidence,
            evidence_manifest=_blob(evidence_bytes),
        )

    def test_runtime_bundle_retains_external_interpreter_truthfully(self) -> None:
        plan = self.plan()
        result = self.result(plan)

        self.assertFalse(plan.standalone)
        self.assertFalse(result.standalone)
        self.assertEqual(PackagePlan.from_dict(plan.to_dict()), plan)
        self.assertEqual(PackageResult.from_dict(result.to_dict()), result)
        self.assertEqual(
            result.runtime_requirements[0].kind,
            RuntimeRequirementKind.INTERPRETER,
        )

    def test_directory_library_package_has_no_product_entrypoint(self) -> None:
        plan = create_package_plan(
            self.graph,
            root_component_revision=self.root_export.component_revision,
            component_lock_identity=_identity("component-lock"),
            target_identity=self.root_export.target_identity,
            root_artifact_identity=self.root_export.identity,
            package_kind=PackageKind.DIRECTORY,
            packager_identity=_identity("packager"),
            destinations=self.destinations,
            entrypoints=(),
            runtime_requirements=(),
        )
        result = self.result(plan)

        self.assertEqual(plan.entrypoints, ())
        self.assertEqual(result.entrypoints, ())
        self.assertEqual(PackagePlan.from_dict(plan.to_dict()), plan)
        self.assertEqual(PackageResult.from_dict(result.to_dict()), result)

        wire = plan.to_dict()
        wire["package_kind"] = PackageKind.RUNTIME_BUNDLE.value
        with self.assertRaisesRegex(
            ContractValidationError, "entrypoints.*must not be empty"
        ):
            PackagePlan.from_dict(wire)

    def test_native_library_archive_retains_exact_layout_without_entrypoint(
        self,
    ) -> None:
        layout = CppLibraryLayout(
            "static",
            ("include/sample/api.hpp",),
            ("lib/libsample.a",),
        )
        plan = create_package_plan(
            self.graph,
            root_component_revision=self.root_export.component_revision,
            component_lock_identity=_identity("component-lock"),
            target_identity=self.root_export.target_identity,
            root_artifact_identity=self.root_export.identity,
            package_kind=PackageKind.ARCHIVE,
            packager_identity=_identity("packager"),
            destinations=self.destinations,
            entrypoints=(),
            native_library_root=self.destinations[self.root_export.identity.uri],
            native_library_layout=layout,
        )
        result = self.result(plan)

        self.assertEqual(PackagePlan.from_dict(plan.to_dict()), plan)
        self.assertEqual(PackageResult.from_dict(result.to_dict()), result)
        self.assertEqual(result.native_library_layout, layout)
        with self.assertRaisesRegex(
            ContractValidationError, "requires both the native library root"
        ):
            replace(plan, native_library_root=None)
        with self.assertRaisesRegex(
            ContractValidationError, "do not declare application entrypoints"
        ):
            replace(plan, entrypoints=(self.entrypoint,))

    def test_standalone_package_rejects_an_external_runtime(self) -> None:
        with self.assertRaisesRegex(
            ContractValidationError, "cannot require an external runtime"
        ):
            self.plan(PackageKind.STANDALONE_EXECUTABLE)

        supplied = replace(self.interpreter, supplied_by_package=True)
        plan = self.plan(PackageKind.STANDALONE_EXECUTABLE, (supplied,))
        result = self.result(plan)
        self.assertTrue(result.standalone)
        wire = result.to_dict()
        wire["standalone"] = False
        with self.assertRaisesRegex(
            ContractValidationError, "does not match the exact runtime closure"
        ):
            PackageResult.from_dict(wire)

    def test_package_plan_retains_link_provenance_without_shipping_static_inputs(
        self,
    ) -> None:
        root_only = {
            self.root_export.identity.uri: self.destinations[
                self.root_export.identity.uri
            ]
        }
        plan = create_package_plan(
            self.graph,
            root_component_revision=self.root_export.component_revision,
            component_lock_identity=_identity("component-lock"),
            target_identity=self.root_export.target_identity,
            root_artifact_identity=self.root_export.identity,
            package_kind=PackageKind.STANDALONE_EXECUTABLE,
            packager_identity=_identity("packager"),
            destinations=root_only,
            entrypoints=(self.entrypoint,),
        )
        self.assertEqual(
            tuple(item.source_identity for item in plan.inputs),
            (self.root_export.identity,),
        )
        self.assertEqual(
            plan.link_plan_identity,
            self.graph.link_plans[0].identity,
        )

    def test_package_plan_rejects_non_linked_or_missing_root_artifacts(self) -> None:
        missing = {
            uri: path
            for uri, path in self.destinations.items()
            if uri != self.root_export.identity.uri
        }
        with self.assertRaisesRegex(ArtifactAssemblyError, "include the linked root"):
            create_package_plan(
                self.graph,
                root_component_revision=self.root_export.component_revision,
                component_lock_identity=_identity("component-lock"),
                target_identity=self.root_export.target_identity,
                root_artifact_identity=self.root_export.identity,
                package_kind=PackageKind.RUNTIME_BUNDLE,
                packager_identity=_identity("packager"),
                destinations=missing,
                entrypoints=(self.entrypoint,),
            )

        forged = dict(self.destinations)
        forged[_identity("not-linked").uri] = "lib/forged"
        with self.assertRaisesRegex(ArtifactAssemblyError, "only exact linked"):
            create_package_plan(
                self.graph,
                root_component_revision=self.root_export.component_revision,
                component_lock_identity=_identity("component-lock"),
                target_identity=self.root_export.target_identity,
                root_artifact_identity=self.root_export.identity,
                package_kind=PackageKind.RUNTIME_BUNDLE,
                packager_identity=_identity("packager"),
                destinations=forged,
                entrypoints=(self.entrypoint,),
            )

    def test_release_resources_are_exact_package_inputs(self) -> None:
        assembly = SourceAssemblyTests()
        assembly.setUp()
        asset = replace(
            assembly.asset(),
            component_revision=self.root_export.component_revision,
            target_identity=self.root_export.target_identity,
        )
        resource = create_resource_package_input(
            asset,
            destination="share/scenes/main.usd",
        )
        plan = create_package_plan(
            self.graph,
            root_component_revision=self.root_export.component_revision,
            component_lock_identity=_identity("component-lock"),
            target_identity=self.root_export.target_identity,
            root_artifact_identity=self.root_export.identity,
            package_kind=PackageKind.RUNTIME_BUNDLE,
            packager_identity=_identity("packager"),
            destinations=self.destinations,
            entrypoints=(self.entrypoint,),
            resource_inputs=(resource,),
            runtime_requirements=(self.interpreter,),
        )
        result = self.result(plan)
        evidence = (_identity("acceptance-receipt"),)
        source_bundle, _source_bytes = _source_bundle("release-root")
        release = ReleaseArtifactSet(
            root_component_ref=ComponentRevisionRef(
                ComponentCoordinate("test", "release-root"),
                "1.0.0",
                plan.root_component_revision,
            ),
            root_component_revision=plan.root_component_revision,
            root_source_bundle=source_bundle,
            component_lock_identity=plan.component_lock_identity,
            target_identity=plan.target_identity,
            artifact_graph_identity=plan.artifact_graph_identity,
            accepted_workspace_identities=(_identity("accepted-workspace"),),
            release_declaration_identities=(_identity("release-declaration"),),
            packages=(result,),
            resource_identities=(resource.source_identity,),
            evidence_identities=evidence,
            evidence_manifest=_blob(standard_release_evidence_manifest_bytes(evidence)),
        )
        self.assertEqual(release.resource_identities, (resource.source_identity,))

    def test_release_artifact_set_binds_packages_to_one_lock_and_target(self) -> None:
        plan = self.plan()
        result = self.result(plan)
        release = self.release(plan, result)

        self.assertEqual(ReleaseArtifactSet.from_dict(release.to_dict()), release)
        with self.assertRaisesRegex(ContractValidationError, "must match"):
            replace(release, target_identity=_identity("other-target"))
        with self.assertRaisesRegex(
            ContractValidationError,
            "every and only declared evidence identity",
        ):
            replace(release, evidence_manifest=_blob(b"unrelated-evidence"))
        with self.assertRaisesRegex(
            ContractValidationError,
            "exactly one declaration for every package result",
        ):
            replace(
                release,
                release_declaration_identities=(
                    *release.release_declaration_identities,
                    _identity("another-release-declaration"),
                ),
            )
        with self.assertRaisesRegex(
            ContractValidationError,
            "canonical tree record",
        ):
            replace(
                release.root_source_bundle,
                root=_blob(b"unrelated-source-tree-record"),
            )

    def test_standard_release_derives_lock_and_evidence_from_accepted_lifecycle(
        self,
    ) -> None:
        lock = _diamond_lock()
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        ports = LifecyclePorts(execution, names)
        lifecycle = _service(ports).execute(
            execution,
            component_lock=lock,
            invalidation=_decision(
                execution,
                names,
                "money",
                tuple(names.values()),
            ),
            prepared_nodes=_prepared_nodes(execution, requests),
            max_parallelism=2,
        )
        self.assertTrue(lifecycle.successful)
        node_results = {
            item.component_revision.uri: item for item in lifecycle.node_results
        }
        manifests = tuple(
            sorted(
                (
                    realize_manifest(
                        ports.plans[revision].manifest,
                        node_results[revision].exports,
                    )
                    for revision in ports.plans
                ),
                key=lambda item: item.component_revision.uri,
            )
        )
        root_export = next(
            export
            for manifest in manifests
            for export in manifest.exports
            if manifest.component_revision == execution.root_revision
        )
        graph = create_artifact_build_graph(
            build_system_driver_identity=manifests[0].build_system_driver_identity,
            manifests=manifests,
            link_roots=(root_export.identity,),
        )
        linked = next(
            item
            for item in graph.link_plans
            if item.root_artifact_identity == root_export.identity
        )
        exports = {
            export.identity.uri: export
            for manifest in graph.manifests
            for export in manifest.exports
        }
        destinations = {
            identity.uri: (
                "bin/app"
                if identity == root_export.identity
                else f"lib/{exports[identity.uri].export_id}.bin"
            )
            for identity in linked.ordered_artifact_identities
        }
        package_plan = create_package_plan(
            graph,
            root_component_revision=execution.root_revision,
            component_lock_identity=execution.component_lock_identity,
            target_identity=root_export.target_identity,
            root_artifact_identity=root_export.identity,
            package_kind=PackageKind.RUNTIME_BUNDLE,
            packager_identity=_identity("standard-packager"),
            destinations=destinations,
            entrypoints=(
                PackageEntrypoint(
                    "app",
                    "application",
                    "bin/app",
                    root_export.identity,
                ),
            ),
        )
        packaged_files = tuple(
            PackagedFile(
                item.path,
                item.role,
                item.kind,
                item.source_identity,
                item.target_identity,
                item.blob,
                item.path == "bin/app",
            )
            for item in package_plan.inputs
        )
        package_result = PackageResult(
            package_plan_identity=package_plan.identity,
            root_component_revision=package_plan.root_component_revision,
            component_lock_identity=package_plan.component_lock_identity,
            target_identity=package_plan.target_identity,
            artifact_graph_identity=package_plan.artifact_graph_identity,
            package_kind=package_plan.package_kind,
            packager_identity=package_plan.packager_identity,
            files=packaged_files,
            artifacts=packaged_files,
            entrypoints=package_plan.entrypoints,
            runtime_requirements=package_plan.runtime_requirements,
        )
        release_declaration = _release_declaration(execution, package_plan)
        source_bundle, source_bundle_bytes = _source_bundle(
            names[execution.root_revision.uri]
        )
        evidence = standard_release_evidence_identities(lifecycle)
        evidence_bytes = standard_release_evidence_manifest_bytes(evidence)
        evidence_manifest = _blob(evidence_bytes)
        blob_bytes = {
            **source_bundle_bytes,
            evidence_manifest.identity: evidence_bytes,
            **{
                export.blob.identity: names[export.component_revision.uri].encode()
                for manifest in graph.manifests
                for export in manifest.exports
            },
        }

        def read_blob(reference: BlobRef) -> bytes:
            return blob_bytes[reference.identity]

        release = create_standard_release_artifact_set(
            execution,
            lifecycle,
            lock,
            graph,
            ((release_declaration, package_result),),
            root_source_bundle=source_bundle,
            evidence_manifest=evidence_manifest,
            read_blob=read_blob,
        )

        self.assertEqual(
            create_standard_artifact_build_graph(execution, lifecycle), graph
        )

        self.assertEqual(
            release.component_lock_identity, execution.component_lock_identity
        )
        self.assertEqual(release.root_component_revision, execution.root_revision)
        self.assertEqual(release.target_identity, root_export.target_identity)
        self.assertEqual(
            release.release_declaration_identities,
            (release_declaration.identity,),
        )
        self.assertIn(lifecycle.identity, release.evidence_identities)
        self.assertIn(
            lifecycle.lifecycle_membership.identity,
            release.evidence_identities,
        )
        root_result = node_results[execution.root_revision.uri]
        self.assertIn(root_result.identity, release.evidence_identities)
        self.assertIn(
            root_result.source_cache_membership.identity,
            release.evidence_identities,
        )
        self.assertEqual(
            len(release.accepted_workspace_identities),
            len(execution.generation_plans),
        )
        forged_driver = _identity("forged-build-system-driver")
        forged_graph = create_artifact_build_graph(
            build_system_driver_identity=forged_driver,
            manifests=tuple(
                replace(manifest, build_system_driver_identity=forged_driver)
                for manifest in graph.manifests
            ),
            link_roots=tuple(item.root_artifact_identity for item in graph.link_plans),
        )
        with self.assertRaisesRegex(
            ReleaseArtifactAssemblyError,
            "retained accepted build-plan authority",
        ):
            create_standard_release_artifact_set(
                execution,
                lifecycle,
                lock,
                forged_graph,
                ((release_declaration, package_result),),
                root_source_bundle=source_bundle,
                evidence_manifest=evidence_manifest,
                read_blob=read_blob,
            )
        with self.assertRaisesRegex(
            ReleaseArtifactAssemblyError,
            "another Component execution plan",
        ):
            create_standard_release_artifact_set(
                replace(execution, planner_identity=_identity("another-planner")),
                lifecycle,
                lock,
                graph,
                ((release_declaration, package_result),),
                root_source_bundle=source_bundle,
                evidence_manifest=evidence_manifest,
                read_blob=read_blob,
            )
        with self.assertRaisesRegex(
            ReleaseArtifactAssemblyError,
            "graph and byte verification",
        ):
            create_standard_release_artifact_set(
                execution,
                lifecycle,
                lock,
                graph,
                (
                    (
                        release_declaration,
                        replace(
                            package_result,
                            component_lock_identity=_identity("foreign-lock"),
                        ),
                    ),
                ),
                root_source_bundle=source_bundle,
                evidence_manifest=evidence_manifest,
                read_blob=read_blob,
            )
        with self.assertRaisesRegex(
            ReleaseArtifactAssemblyError,
            "release declaration differs",
        ):
            create_standard_release_artifact_set(
                execution,
                lifecycle,
                lock,
                graph,
                (
                    (
                        replace(
                            release_declaration,
                            component_lock_identity=_identity("foreign-lock"),
                        ),
                        package_result,
                    ),
                ),
                root_source_bundle=source_bundle,
                evidence_manifest=evidence_manifest,
                read_blob=read_blob,
            )
        foreign_artifact_bytes = b"foreign-package-artifact"
        foreign_artifact = replace(
            package_result.artifacts[0],
            blob=_blob(foreign_artifact_bytes, "application/x-native"),
        )
        blob_bytes[foreign_artifact.blob.identity] = foreign_artifact_bytes
        with self.assertRaisesRegex(
            ReleaseArtifactAssemblyError,
            "graph and byte verification",
        ):
            create_standard_release_artifact_set(
                execution,
                lifecycle,
                lock,
                graph,
                (
                    (
                        release_declaration,
                        replace(
                            package_result,
                            artifacts=(
                                foreign_artifact,
                                *package_result.artifacts[1:],
                            ),
                        ),
                    ),
                ),
                root_source_bundle=source_bundle,
                evidence_manifest=evidence_manifest,
                read_blob=read_blob,
            )
        with self.assertRaisesRegex(
            ReleaseArtifactAssemblyError,
            "evidence manifest differs",
        ):
            create_standard_release_artifact_set(
                execution,
                lifecycle,
                lock,
                graph,
                ((release_declaration, package_result),),
                root_source_bundle=source_bundle,
                evidence_manifest=_blob(b"substituted-evidence"),
                read_blob=read_blob,
            )
        with self.assertRaisesRegex(
            ReleaseArtifactAssemblyError,
            "root source bundle does not match",
        ):
            create_standard_release_artifact_set(
                execution,
                lifecycle,
                lock,
                graph,
                ((release_declaration, package_result),),
                root_source_bundle=_source_bundle("substituted-source")[0],
                evidence_manifest=evidence_manifest,
                read_blob=read_blob,
            )

        source_file = source_bundle.files[0].blob
        source_file_bytes = blob_bytes[source_file.identity]

        def read_tampered_source(reference: BlobRef) -> bytes:
            if reference == source_file:
                return b"x" * len(source_file_bytes)
            return read_blob(reference)

        with self.assertRaisesRegex(
            ReleaseArtifactAssemblyError,
            "failed byte verification",
        ):
            create_standard_release_artifact_set(
                execution,
                lifecycle,
                lock,
                graph,
                ((release_declaration, package_result),),
                root_source_bundle=source_bundle,
                evidence_manifest=evidence_manifest,
                read_blob=read_tampered_source,
            )

        publication_target = _identity("publication-target")
        request = create_publication_request_from_release(
            release,
            component_lock=lock,
            security_classification_identity=_identity("security-classification"),
            security_profile=SecurityProfile.CONSTRAINED,
            target_id="local-release-cache",
            target_identity=publication_target,
            policy_identity=_identity("publication-policy"),
            actor="release-test",
        )

        self.assertEqual(
            request.component_lock_identity, release.component_lock_identity
        )
        self.assertEqual(request.revision, release.root_component_revision.uri)
        self.assertEqual(request.target_identity_digest, publication_target.uri)
        self.assertEqual(request.source_bundle, release.root_source_bundle.root)
        self.assertEqual(request.provenance, (release.evidence_manifest,))
        self.assertIn(release.evidence_manifest, request.blobs)
        self.assertTrue(
            {item.blob.identity for item in release.root_source_bundle.files}.issubset(
                {item.identity for item in request.blobs}
            )
        )
        self.assertEqual(
            set(request.roots.values()),
            {
                release.root_source_bundle.root,
                *(
                    item.blob
                    for package in release.packages
                    for item in package.artifacts
                ),
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            publication_root = Path(temporary)
            publication_target = FilesystemPublicationTarget(
                "release-cache", publication_root / "published"
            )
            publication_policy = PublicationPolicy(
                _identity("publication-policy").uri,
                (publication_target.target_id,),
            )
            rejected_cas = FileSystemCAS(publication_root / "rejected-cas")
            rejected_policy = PublicationPolicy(
                _identity("rejected-publication-policy").uri,
                ("another-target",),
            )
            rejected_service = StandardProjectReleaseService(
                PublicationService(
                    rejected_cas,
                    AppendOnlyEventStore(publication_root / "rejected-events"),
                    rejected_policy,
                    clock=lambda: datetime(2026, 8, 7, 12, 1, tzinfo=UTC),
                ),
                rejected_policy,
                publication_target,
                {PackageKind.RUNTIME_BUNDLE: DirectoryPackageAdapter()},
            )
            with self.assertRaisesRegex(PublicationError, "not permitted"):
                rejected_service.publish_accepted(
                    execution,
                    lifecycle,
                    lock,
                    (release_declaration,),
                    root_source_bundle=source_bundle,
                    evidence_manifest=evidence_manifest,
                    read_blob=read_blob,
                    security_classification_identity=_identity(
                        "security-classification"
                    ),
                    security_profile=SecurityProfile.CONSTRAINED,
                    actor="release-test",
                    reason="reject unauthorized test release",
                    now=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
                )
            self.assertTrue(
                all(not rejected_cas.contains(reference) for reference in request.blobs)
            )
            release_service = StandardProjectReleaseService(
                PublicationService(
                    FileSystemCAS(publication_root / "cas"),
                    AppendOnlyEventStore(publication_root / "events"),
                    publication_policy,
                    clock=lambda: datetime(2026, 8, 7, 12, 1, tzinfo=UTC),
                ),
                publication_policy,
                publication_target,
                {PackageKind.RUNTIME_BUNDLE: DirectoryPackageAdapter()},
            )
            published = release_service.publish_accepted(
                execution,
                lifecycle,
                lock,
                (release_declaration,),
                root_source_bundle=source_bundle,
                evidence_manifest=evidence_manifest,
                read_blob=read_blob,
                security_classification_identity=_identity("security-classification"),
                security_profile=SecurityProfile.CONSTRAINED,
                actor="release-test",
                reason="publish accepted test release",
                now=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
            )
            self.assertEqual(published.receipt.component_lock_identity, lock.identity)
            self.assertEqual(
                published.transfer_receipt.publication_request_digest,
                published.request.digest,
            )
            self.assertTrue(
                all(
                    publication_target.has_blob(reference)
                    for reference in published.request.blobs
                )
            )
            imported = FileSystemCAS(publication_root / "fresh-import-cas")
            for reference in (
                source_bundle.root,
                *(item.blob for item in source_bundle.files),
            ):
                self.assertTrue(publication_target.import_blob(imported, reference))
            reconstructed = publication_root / "reconstructed-source"
            for item in source_bundle.files:
                destination = reconstructed / item.path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(imported.get_bytes(item.blob))
                self.assertEqual(
                    destination.read_bytes(), source_bundle_bytes[item.blob.identity]
                )
            self.assertEqual(
                imported.get_bytes(source_bundle.root),
                source_bundle_bytes[source_bundle.root.identity],
            )
        with self.assertRaisesRegex(
            ReleasePublicationError,
            "Component lock differs",
        ):
            create_publication_request_from_release(
                release,
                component_lock=replace(
                    lock,
                    resolver_identity=_identity("foreign-lock-resolver"),
                ),
                security_classification_identity=_identity("security-classification"),
                security_profile=SecurityProfile.CONSTRAINED,
                target_id="local-release-cache",
                target_identity=publication_target,
                policy_identity=_identity("publication-policy"),
                actor="release-test",
            )
        with self.assertRaisesRegex(
            ReleasePublicationError,
            "Component ref differs",
        ):
            create_publication_request_from_release(
                replace(
                    release,
                    root_component_ref=ComponentRevisionRef(
                        ComponentCoordinate("foreign", "relabelled"),
                        "9.9.9",
                        release.root_component_revision,
                    ),
                ),
                component_lock=lock,
                security_classification_identity=_identity("security-classification"),
                security_profile=SecurityProfile.CONSTRAINED,
                target_id="local-release-cache",
                target_identity=publication_target,
                policy_identity=_identity("publication-policy"),
                actor="release-test",
            )


if __name__ == "__main__":
    unittest.main()

from literate_ai.contracts.executable_components.packages import (
    PackageKind,  # noqa: F401
)
# NOTE: names not defined at top level of tests.unit.test_package_release_contracts: ['PackageKind', 'PackagePlan', 'PackageResult']
