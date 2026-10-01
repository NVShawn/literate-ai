"""Published v2 wire conformance for planning, context, and artifact contracts."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from literate_ai.application.artifact_graph import (
    assemble_source_tree,
    create_artifact_build_graph,
    create_composite_build_request,
    plan_isolated_materialization,
)
from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.application.component_generation_context import (
    prepare_component_generation_context,
)
from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.contracts.executable_components.artifacts import (
    ArtifactAssemblyDependency,
    ArtifactExportDeclaration,
    ArtifactMaterializationPlan,
    BuildPrivilege,
    BuildSubActionKind,
    ComponentBuildManifest,
)
from tests.unit import test_artifact_graph_contracts as artifact_fixtures
from tests.unit import test_component_execution_planning as planning_fixtures
from tests.unit import test_component_generation_context as context_fixtures
from tests.unit import test_package_release_contracts as package_fixtures
from tests.unit.test_schema_catalog import SchemaCatalog

ROOT = Path(__file__).resolve().parents[2]
V2_ROOT = ROOT / "schemas" / "v2"


def _official_validator(resource_id: str) -> Draft202012Validator:
    registry = Registry()
    for directory in (ROOT / "schemas" / "v1", V2_ROOT):
        for path in sorted(directory.glob("*.schema.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            registry = registry.with_resource(
                document["$id"], Resource.from_contents(document)
            )
    return Draft202012Validator({"$ref": resource_id}, registry=registry.crawl())


def _planning_contracts() -> tuple[object, ...]:
    lock = planning_fixtures._diamond_lock()
    execution = plan_component_execution(
        lock, model_identities=planning_fixtures._models(lock)
    )
    return (
        execution,
        *(item.generation_key for item in execution.generation_plans),
        *execution.generation_plans,
        *(layer for action in execution.action_plans for layer in action.layers),
        *execution.action_plans,
    )


def _context_contracts() -> tuple[object, ...]:
    plan, inputs = context_fixtures._materialize(
        context_fixtures._named_plan(planning_fixtures._diamond_lock(), "invoice-cli")
    )
    prepared = prepare_component_generation_context(
        plan,
        framework_envelope=b"trusted schema fixture envelope",
        authority_segments=inputs,
        budget=context_fixtures._budget(),
    )
    request = prepared.request
    return (
        request.context_manifest,
        request.budget,
        request.budget_decision,
        request,
    )


def _artifact_contracts() -> tuple[object, ...]:
    assembly = artifact_fixtures.SourceAssemblyTests()
    assembly.setUp()
    generated = assembly.generated()
    manifest = assemble_source_tree(
        generated, (assembly.asset(),), read_blob=assembly.read
    )
    materialization = plan_isolated_materialization(
        manifest, execution_nonce=assembly.authorization
    )

    fixture = artifact_fixtures.ArtifactGraphTests()
    manifests = fixture.diamond()
    root_export = manifests[0].exports[0]
    graph = create_artifact_build_graph(
        build_system_driver_identity=fixture.driver,
        manifests=manifests,
        link_roots=(root_export.identity,),
    )
    first_manifest = graph.manifests[0]
    first_action = first_manifest.actions[0]
    composite_materialization = ArtifactMaterializationPlan(
        first_manifest.source_tree_identity,
        artifact_fixtures.identity("schema-composite-execution-root"),
        (),
    )
    composite = create_composite_build_request(
        first_manifest,
        composite_materialization,
        build_system_resolver_identity=artifact_fixtures.identity(
            "schema-build-system-resolver"
        ),
        language_compiler_identity=first_action.toolchain_identity,
        language_runtime_identity=artifact_fixtures.identity("schema-language-runtime"),
        ordered_actions=((BuildSubActionKind.COMPILE, first_action.action_id),),
        requested_privileges=(BuildPrivilege.EXECUTE_BUILD_TOOLS,),
    )
    package_fixture = package_fixtures.PackageReleaseContractTests()
    package_fixture.setUp()
    package_plan = package_fixture.plan()
    package_result = package_fixture.result(package_plan)
    release_artifacts = package_fixture.release(package_plan, package_result)
    return (
        generated,
        manifest,
        *(
            declaration
            for item in graph.manifests
            for declaration in item.export_declarations
        ),
        *(export for item in graph.manifests for export in item.exports),
        *(action for item in graph.manifests for action in item.actions),
        *graph.manifests,
        *graph.link_plans,
        materialization,
        composite,
        graph,
        package_plan,
        package_result,
        release_artifacts,
        *(
            ArtifactAssemblyDependency(
                root_export.identity,
                next(
                    export.identity
                    for item in manifests
                    for export in item.exports
                    if export.identity != root_export.identity
                ),
                kind,
                artifact_fixtures.identity("schema-late-edge"),
                artifact_fixtures.identity("schema-provider-acceptance"),
            )
            for kind in (DependencyKind.RUNTIME, DependencyKind.PACKAGING)
        ),
    )


class ExecutableComponentV2SchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaCatalog()

    def test_every_top_level_contract_wire_validates_and_is_closed(self) -> None:
        contracts = (
            *_planning_contracts(),
            *_context_contracts(),
            *_artifact_contracts(),
        )
        observed: set[str] = set()
        for contract in contracts:
            with self.subTest(contract=type(contract).__name__):
                document = contract.to_dict()
                schema = document["schema"]
                observed.add(schema)
                self.schemas.validate(schema, document)
                with self.assertRaisesRegex(AssertionError, "unknown field"):
                    self.schemas.validate(schema, {**document, "unexpected": True})

        catalog = json.loads((V2_ROOT / "index.json").read_text(encoding="utf-8"))
        published = {
            identifier
            for entry in catalog["schemas"]
            if entry["file"]
            in {
                "component-artifacts.schema.json",
                "component-execution-planning.schema.json",
                "component-generation-context.schema.json",
            }
            for identifier in entry["public_ids"]
        }
        self.assertEqual(observed, published)

    def test_build_manifest_accepts_old_v2_wire_and_canonicalizes_declarations(
        self,
    ) -> None:
        fixture = artifact_fixtures.ArtifactGraphTests()
        fixture.setUp()
        manifest = fixture.diamond()[0]
        old_wire = manifest.to_dict()
        del old_wire["export_declarations"]

        self.schemas.validate(ComponentBuildManifest.SCHEMA, old_wire)
        decoded = ComponentBuildManifest.from_dict(old_wire)
        self.assertEqual(
            decoded.export_declarations,
            tuple(item.declaration for item in decoded.exports),
        )
        self.assertIn("export_declarations", decoded.to_dict())
        with self.assertRaisesRegex(ValueError, "ArtifactExportDeclaration values"):
            ComponentBuildManifest(
                manifest.component_revision,
                manifest.source_tree_identity,
                manifest.build_system_driver_identity,
                manifest.actions,
                (),
                (manifest.exports[0],),
            )
        self.assertIs(type(decoded.export_declarations[0]), ArtifactExportDeclaration)

    def test_standalone_package_wire_cannot_deny_its_runtime_closure(self) -> None:
        fixture = package_fixtures.PackageReleaseContractTests()
        fixture.setUp()
        plan = fixture.plan(
            package_fixtures.PackageKind.STANDALONE_EXECUTABLE,
            (),
        )
        wire = fixture.result(plan).to_dict()
        wire["standalone"] = False

        self.assertFalse(
            _official_validator(package_fixtures.PackageResult.SCHEMA).is_valid(wire)
        )

    def test_directory_library_wire_may_omit_product_entrypoints_only(self) -> None:
        fixture = package_fixtures.PackageReleaseContractTests()
        fixture.setUp()
        original_plan = fixture.plan()
        plan = original_plan.to_dict()
        plan["package_kind"] = package_fixtures.PackageKind.DIRECTORY.value
        plan["entrypoints"] = []
        plan["runtime_requirements"] = []
        result = fixture.result(original_plan).to_dict()
        result["package_kind"] = package_fixtures.PackageKind.DIRECTORY.value
        result["entrypoints"] = []
        result["runtime_requirements"] = []
        result["standalone"] = False

        self.assertTrue(
            _official_validator(package_fixtures.PackagePlan.SCHEMA).is_valid(plan)
        )
        self.assertTrue(
            _official_validator(package_fixtures.PackageResult.SCHEMA).is_valid(result)
        )
        plan["package_kind"] = package_fixtures.PackageKind.RUNTIME_BUNDLE.value
        result["package_kind"] = package_fixtures.PackageKind.RUNTIME_BUNDLE.value
        self.assertFalse(
            _official_validator(package_fixtures.PackagePlan.SCHEMA).is_valid(plan)
        )
        self.assertFalse(
            _official_validator(package_fixtures.PackageResult.SCHEMA).is_valid(result)
        )

    def test_v2_catalog_lists_every_file_and_resource_and_resolves_refs(self) -> None:
        catalog = json.loads((V2_ROOT / "index.json").read_text(encoding="utf-8"))
        files = {path.name for path in V2_ROOT.glob("*.schema.json")}
        self.assertEqual(files, {entry["file"] for entry in catalog["schemas"]})

        resources = {
            identifier
            for entry in catalog["schemas"]
            for identifier in (entry["root_id"], *entry["public_ids"])
        }
        schemas = SchemaCatalog(V2_ROOT)
        self.assertEqual(resources, set(schemas.resources))
        combined = SchemaCatalog()

        def walk(value: object, base: str | None = None) -> None:
            if isinstance(value, dict):
                base = value.get("$id", base)
                if "$ref" in value:
                    combined.resolve(value["$ref"], base)
                if value.get("type") == "object" and "properties" in value:
                    self.assertIs(value.get("additionalProperties"), False)
                for child in value.values():
                    walk(child, base)
            elif isinstance(value, list):
                for child in value:
                    walk(child, base)

        for path in V2_ROOT.glob("*.schema.json"):
            walk(json.loads(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
