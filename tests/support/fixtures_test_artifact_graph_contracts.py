"""Shared test fixtures extracted from test_artifact_graph_contracts."""

from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace

from literate_ai.application.artifact_graph import (
    ArtifactAssemblyError,
    assemble_source_tree,
    create_artifact_build_graph,
    plan_isolated_materialization,
)
from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.contracts.executable_components.artifacts import (
    ArtifactAssemblyDependency,
    ArtifactBuildGraph,
    ArtifactExport,
    BuildActionRequest,
    ComponentBuildManifest,
    GeneratedTextFile,
    GeneratedTextTree,
    SourceTreeEntryOrigin,
)
from literate_ai.contracts.executable_components.assets import AuthoredBinaryAsset
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


def identity(label: str) -> ContentIdentity:
    return canonical_identity({"fixture": label})


def blob(content: bytes, media_type: str) -> BlobRef:
    return BlobRef(
        hashlib.sha256(content).hexdigest(), len(content), media_type=media_type
    )


class SourceAssemblyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.component = identity("component")
        self.target = identity("target")
        self.authorization = identity("authorization")
        self.text = b"def answer():\n    return 42\n"
        self.icon = b"\x89PNG\r\n\x1a\n"
        self.text_blob = blob(self.text, "text/x-python")
        self.icon_blob = blob(self.icon, "image/png")
        self.contents = {
            self.text_blob.identity: self.text,
            self.icon_blob.identity: self.icon,
        }

    def generated(self, path: str = "src/app.py") -> GeneratedTextTree:
        return GeneratedTextTree(
            self.component,
            self.target,
            self.authorization,
            (GeneratedTextFile(path, self.text_blob),),
        )

    def asset(self, path: str = "assets/icon.png") -> AuthoredBinaryAsset:
        return AuthoredBinaryAsset(
            component_revision=self.component,
            asset_id="icon",
            path=path,
            role="runtime-asset",
            target_identity=self.target,
            blob=self.icon_blob,
            authorization_identity=self.authorization,
        )

    def read(self, reference: BlobRef) -> bytes:
        return self.contents[reference.identity]

    def test_assembles_verified_text_and_binary_blobs_deterministically(self) -> None:
        manifest = assemble_source_tree(
            self.generated(), (self.asset(),), read_blob=self.read
        )
        repeated = assemble_source_tree(
            self.generated(), reversed((self.asset(),)), read_blob=self.read
        )
        self.assertEqual(manifest.identity, repeated.identity)
        self.assertEqual(
            tuple(item.origin for item in manifest.entries),
            (
                SourceTreeEntryOrigin.AUTHORED_BINARY,
                SourceTreeEntryOrigin.GENERATED_TEXT,
            ),
        )
        self.assertEqual(self.read(manifest.entries[0].blob), self.icon)

    def test_model_cannot_overwrite_content_locked_asset(self) -> None:
        with self.assertRaisesRegex(ArtifactAssemblyError, "cannot overwrite"):
            assemble_source_tree(
                self.generated("assets/icon.png"), (self.asset(),), read_blob=self.read
            )

    def test_rejects_wrong_blob_bytes_and_non_utf8_model_output(self) -> None:
        with self.assertRaisesRegex(ArtifactAssemblyError, "digest mismatch"):
            assemble_source_tree(
                self.generated(),
                (),
                read_blob=lambda _: b"same-length-wrong-bytes........."[
                    : len(self.text)
                ],
            )
        bad = b"\xff"
        bad_blob = blob(bad, "text/plain")
        generated = GeneratedTextTree(
            self.component,
            self.target,
            self.authorization,
            (GeneratedTextFile("bad.txt", bad_blob),),
        )
        with self.assertRaisesRegex(ArtifactAssemblyError, "not UTF-8"):
            assemble_source_tree(generated, (), read_blob=lambda _: bad)

    def test_materialization_plan_contains_exact_blobs_and_no_host_paths(self) -> None:
        manifest = assemble_source_tree(
            self.generated(), (self.asset(),), read_blob=self.read
        )
        plan = plan_isolated_materialization(
            manifest, execution_nonce=identity("run-1")
        )
        self.assertEqual(plan.source_tree_identity, manifest.identity)
        self.assertEqual(plan.entries, manifest.entries)
        document = plan.to_dict()
        self.assertNotIn("host_path", repr(document))
        self.assertIn("fresh-empty-exact-blobs", repr(document))


class ArtifactGraphTests(unittest.TestCase):
    target = identity("linux-x86_64")
    abi = identity("abi-v1")
    toolchain = identity("clang-20")
    authorization = identity("build-authorization")
    producer = identity("native-compiler")
    driver = identity("build-driver")

    def export(
        self,
        name: str,
        dependencies: tuple[ArtifactExport, ...] = (),
    ) -> ArtifactExport:
        payload = name.encode()
        return ArtifactExport(
            export_id=name,
            component_revision=identity(f"component-{name}"),
            role="static-library" if name != "app" else "executable",
            abi_identity=self.abi,
            target_identity=self.target,
            media_type="application/x-native",
            producer_identity=self.producer,
            source_tree_identity=identity(f"source-{name}"),
            toolchain_identity=self.toolchain,
            authorization_identity=self.authorization,
            dependency_artifact_identities=tuple(
                sorted(
                    (item.identity for item in dependencies), key=lambda item: item.uri
                )
            ),
            blob=blob(payload, "application/x-native"),
        )

    def manifest(
        self,
        export: ArtifactExport,
        dependencies: tuple[ArtifactExport, ...] = (),
        package_dependencies: tuple[ArtifactExport, ...] = (),
    ) -> ComponentBuildManifest:
        ordered = tuple(sorted(dependencies, key=lambda item: item.identity.uri))
        ordered_package = tuple(
            sorted(package_dependencies, key=lambda item: item.identity.uri)
        )
        action = BuildActionRequest(
            action_id=f"build-{export.export_id}",
            component_revision=export.component_revision,
            role=export.role,
            abi_identity=export.abi_identity,
            target_identity=export.target_identity,
            media_type=export.media_type,
            producer_identity=export.producer_identity,
            source_tree_identity=export.source_tree_identity,
            toolchain_identity=export.toolchain_identity,
            authorization_identity=export.authorization_identity,
            dependency_artifacts=ordered,
            declared_output_ids=(export.export_id,),
            package_dependency_artifacts=ordered_package,
        )
        return ComponentBuildManifest(
            export.component_revision,
            export.source_tree_identity,
            self.driver,
            (action,),
            (export,),
        )

    def diamond(self) -> tuple[ComponentBuildManifest, ...]:
        common = self.export("common")
        left = self.export("left", (common,))
        right = self.export("right", (common,))
        app = self.export("app", (left, right))
        return (
            self.manifest(app, (left, right)),
            self.manifest(left, (common,)),
            self.manifest(right, (common,)),
            self.manifest(common),
        )

    def test_late_assembly_dependencies_preserve_compilation_and_expand_closure(self):
        app, runtime, resource = (
            self.export(name) for name in ("app", "runtime", "resource")
        )
        manifests = tuple(self.manifest(item) for item in (app, runtime, resource))
        edges = (
            ArtifactAssemblyDependency(
                app.identity,
                runtime.identity,
                DependencyKind.RUNTIME,
                identity("runtime-edge"),
                identity("runtime-acceptance"),
            ),
            ArtifactAssemblyDependency(
                runtime.identity,
                resource.identity,
                DependencyKind.PACKAGING,
                identity("package-edge"),
                identity("resource-acceptance"),
            ),
        )
        graph = create_artifact_build_graph(
            build_system_driver_identity=self.driver,
            manifests=manifests,
            link_roots=(app.identity,),
            assembly_dependencies=edges,
        )
        self.assertEqual(
            set(graph.link_plans[0].ordered_artifact_identities),
            {app.identity, runtime.identity, resource.identity},
        )
        self.assertEqual(
            graph.link_plans[0].resolved_root_artifact_identities, (app.identity,)
        )
        self.assertEqual(set(graph.manifests), set(manifests))
        self.assertFalse(app.dependency_artifact_identities)
        self.assertEqual(ArtifactBuildGraph.from_dict(graph.to_dict()), graph)
        SchemaCatalog().validate(graph.SCHEMA, graph.to_dict())
        for edge in edges:
            SchemaCatalog().validate(edge.SCHEMA, edge.to_dict())
        for kind in (DependencyKind.BUILD, DependencyKind.DEPLOYMENT):
            with self.subTest(kind=kind), self.assertRaises(ContractValidationError):
                replace(edges[0], dependency_kind=kind)
        changed = tuple(
            sorted(
                (
                    replace(
                        edges[0],
                        provider_acceptance_identity=identity("other-acceptance"),
                    ),
                    edges[1],
                ),
                key=lambda item: item.identity.uri,
            )
        )
        self.assertNotEqual(
            replace(graph, assembly_dependencies=changed).identity, graph.identity
        )
        reordered = create_artifact_build_graph(
            build_system_driver_identity=self.driver,
            manifests=reversed(manifests),
            link_roots=(app.identity,),
            assembly_dependencies=reversed(edges),
        )
        self.assertEqual(graph.identity, reordered.identity)
        with self.assertRaisesRegex(ContractValidationError, "exact artifact closure"):
            replace(
                graph,
                link_plans=(
                    replace(
                        graph.link_plans[0], ordered_artifact_identities=(app.identity,)
                    ),
                ),
            )
        for invalid in (
            (edges[0], edges[0]),
            (replace(edges[0], provider_artifact_identity=identity("absent")),),
            (
                *edges,
                ArtifactAssemblyDependency(
                    resource.identity,
                    app.identity,
                    DependencyKind.RUNTIME,
                    identity("cycle-edge"),
                    identity("app-acceptance"),
                ),
            ),
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises((ArtifactAssemblyError, ContractValidationError)),
            ):
                create_artifact_build_graph(
                    build_system_driver_identity=self.driver,
                    manifests=manifests,
                    link_roots=(app.identity,),
                    assembly_dependencies=invalid,
                )

    def test_diamond_link_plan_is_exact_deduplicated_and_deterministic(self) -> None:
        manifests = self.diamond()
        app = manifests[0].exports[0]
        graph = create_artifact_build_graph(
            build_system_driver_identity=self.driver,
            manifests=manifests,
            link_roots=(app.identity,),
        )
        reordered = create_artifact_build_graph(
            build_system_driver_identity=self.driver,
            manifests=reversed(manifests),
            link_roots=(app.identity,),
        )
        self.assertEqual(graph.identity, reordered.identity)
        self.assertNotIn("assembly_dependencies", graph.to_dict())
        self.assertEqual(len(graph.link_plans[0].ordered_artifact_identities), 4)
        self.assertEqual(len(set(graph.link_plans[0].ordered_artifact_identities)), 4)
        self.assertEqual(ArtifactBuildGraph.from_dict(graph.to_dict()), graph)

    def test_one_action_and_grouped_link_plan_bind_multiple_root_exports(self) -> None:
        app = self.export("app")
        worker = replace(
            self.export("worker"),
            component_revision=app.component_revision,
            source_tree_identity=app.source_tree_identity,
        )
        declarations = (app.declaration, worker.declaration)
        action = BuildActionRequest(
            action_id="build-multiple-entrypoints",
            component_revision=app.component_revision,
            role=app.role,
            abi_identity=app.abi_identity,
            target_identity=app.target_identity,
            media_type=app.media_type,
            producer_identity=app.producer_identity,
            source_tree_identity=app.source_tree_identity,
            toolchain_identity=app.toolchain_identity,
            authorization_identity=app.authorization_identity,
            dependency_artifacts=(),
            declared_output_ids=("app", "worker"),
            output_declarations=declarations,
        )
        manifest = ComponentBuildManifest(
            app.component_revision,
            app.source_tree_identity,
            self.driver,
            (action,),
            (app, worker),
        )

        graph = create_artifact_build_graph(
            build_system_driver_identity=self.driver,
            manifests=(manifest,),
            link_roots=(),
            link_root_groups=((app.identity, worker.identity),),
        )

        link = graph.link_plans[0]
        self.assertEqual(
            set(link.resolved_root_artifact_identities),
            {app.identity, worker.identity},
        )
        self.assertEqual(
            set(link.ordered_artifact_identities),
            {app.identity, worker.identity},
        )
        self.assertEqual(ArtifactBuildGraph.from_dict(graph.to_dict()), graph)
        self.assertEqual(
            BuildActionRequest.from_dict(action.to_dict()),
            action,
        )

    def test_multi_output_action_rejects_partial_or_rewritten_declarations(
        self,
    ) -> None:
        app = self.export("app")
        worker = replace(
            self.export("worker"),
            component_revision=app.component_revision,
            source_tree_identity=app.source_tree_identity,
        )
        arguments = dict(
            action_id="build-multiple-entrypoints",
            component_revision=app.component_revision,
            role=app.role,
            abi_identity=app.abi_identity,
            target_identity=app.target_identity,
            media_type=app.media_type,
            producer_identity=app.producer_identity,
            source_tree_identity=app.source_tree_identity,
            toolchain_identity=app.toolchain_identity,
            authorization_identity=app.authorization_identity,
            dependency_artifacts=(),
            declared_output_ids=("app", "worker"),
        )
        with self.assertRaisesRegex(ContractValidationError, "exactly describe"):
            BuildActionRequest(
                **arguments,
                output_declarations=(app.declaration,),
            )
        with self.assertRaisesRegex(ContractValidationError, "must share"):
            BuildActionRequest(
                **arguments,
                output_declarations=(
                    app.declaration,
                    replace(
                        worker.declaration,
                        source_tree_identity=identity("rewritten"),
                    ),
                ),
            )

    def test_packaging_dependencies_round_trip_without_becoming_build_inputs(
        self,
    ) -> None:
        package = self.export("package-resource")
        app = self.export("app", (package,))
        app_manifest = self.manifest(app, (), (package,))
        graph = create_artifact_build_graph(
            build_system_driver_identity=self.driver,
            manifests=(app_manifest, self.manifest(package)),
            link_roots=(app.identity,),
        )

        action = next(
            manifest.actions[0]
            for manifest in graph.manifests
            if manifest.component_revision == app.component_revision
        )
        self.assertEqual(action.dependency_artifacts, ())
        self.assertEqual(action.package_dependency_artifacts, (package,))
        self.assertIn("package_dependency_artifacts", action.to_dict())
        self.assertEqual(ArtifactBuildGraph.from_dict(graph.to_dict()), graph)

    def test_manifest_rejects_wrong_producer_abi_role_or_target(self) -> None:
        export = self.export("library")
        manifest = self.manifest(export)
        action = manifest.actions[0]
        for field, value in (
            ("producer_identity", identity("other-producer")),
            ("abi_identity", identity("other-abi")),
            ("role", "shared-library"),
            ("target_identity", identity("other-target")),
        ):
            with self.subTest(field=field), self.assertRaises(ContractValidationError):
                ComponentBuildManifest(
                    export.component_revision,
                    export.source_tree_identity,
                    self.driver,
                    (replace(action, **{field: value}),),
                    (export,),
                )

    def test_unrealized_action_dependency_requires_graph_custody(self):
        absent = self.export("absent")
        root = self.export("root", (absent,))
        manifest = replace(self.manifest(root, (absent,)), exports=())
        with self.assertRaisesRegex(
            ContractValidationError, "action dependency is absent"
        ):
            ArtifactBuildGraph(self.driver, (manifest,), ())

    def test_missing_dependency_fails_and_driver_is_not_hardcoded(self) -> None:
        absent = self.export("absent")
        root = self.export("root", (absent,))
        manifest = self.manifest(root, (absent,))
        with self.assertRaisesRegex(ArtifactAssemblyError, "artifact is absent"):
            create_artifact_build_graph(
                build_system_driver_identity=self.driver,
                manifests=(manifest,),
                link_roots=(root.identity,),
            )
        alternative = identity("mix-driver")
        alternative_manifest = replace(
            manifest, build_system_driver_identity=alternative
        )
        complete = self.manifest(absent)
        alternative_complete = replace(
            complete, build_system_driver_identity=alternative
        )
        graph = create_artifact_build_graph(
            build_system_driver_identity=alternative,
            manifests=(alternative_manifest, alternative_complete),
            link_roots=(root.identity,),
        )
        self.assertEqual(graph.build_system_driver_identity, alternative)


if __name__ == "__main__":
    unittest.main()
