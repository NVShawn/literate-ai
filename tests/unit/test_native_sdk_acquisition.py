"""SDK acquisition discovers real tools and feeds locked generation preparation."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.component_acceptance import (
    FilesystemComponentAcceptanceOracle,
)
from literate_ai.adapters.dependencies import build_cyclonedx_bom
from literate_ai.adapters.generation_preparation import (
    FilesystemLockedGenerationApplicationAdapter,
    FilesystemLockedGenerationPreparationAdapter,
)
from literate_ai.adapters.lifecycle.standard_local import (
    local_generated_source_tree_identity,
)
from literate_ai.adapters.locked_generation_authority import (
    LockedGenerationAuthorityReaderError,
)
from literate_ai.adapters.models.coding_cli import (
    _acceptance_argument_vectors,
    _acceptance_result_shape,
    _recipe_authority_sbom,
)
from literate_ai.adapters.native_sdk_acquisition import NativeSdkProjectAcquisition
from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.adapters.native_sdk_generation import native_sdk_generation_identities
from literate_ai.adapters.source.repository_cache import (
    RepositorySourceCachePolicyError,
)
from literate_ai.adapters.standard_project import (
    PlannedStandardProject,
    StandardProjectExecutionRequest,
    assemble_filesystem_standard_project_runtime,
    project_locked_standard_toolchain_closure,
)
from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.application.generation_preparation import (
    GenerationPreparationError,
    GenerationPreparationRequest,
)
from literate_ai.contracts import SourceIntelligenceMode
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from literate_ai.contracts.executable_components import (
    ComponentChangeSurface,
    ComponentCommandPhase,
    ComponentCommandRole,
    ComponentInvalidationDecision,
    GeneratedSourceCandidate,
    SourceGenerationProvenance,
    SourceGenerationRunOutput,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.sbom import CycloneDxLifecycle
from literate_ai.generated_tests import (
    GENERATED_TEST_SUITE_PATH,
    GENERATED_TEST_SUITE_SCHEMA,
    MAJOR_REBUILD_GENERATION_MODE,
    validate_generated_test_suite,
)
from literate_ai.security import AuthorizationError, SecurityPolicy
from tests.support import fixtures_test_native_sdk_source_build as test_native_sdk_source_build
from tests.support.fixtures_test_component_node_generation_preparation import _budget
from tests.support.fixtures_test_native_sdk_preparation import preparation_recipe
from tests.support.fixtures_test_repository_sources import source_intelligence_policy
from tests.support.fixtures_test_standard_command_projection import _observation, _tool
from tests.support.fixtures_test_standard_project_factory import _selection

_SDK_CASES = (
    ("scale-example", 2, 4, "8", "example"),
    ("scale-boundary", -3, 2, "-6", "boundary"),
    ("scale-invariant", 0, 99, "0", "invariant"),
)


def _native_sdk_lifecycle_recipe(fixture) -> None:
    """Make the fixture's product contract exercise its admitted scale SDK."""

    (fixture.component.parent / "skills/implement.json").write_text(
        json.dumps(
            {
                "schema": "urn:literate-ai:schema:v1:specification-to-source-skill",
                "skill_id": "fixture-implementation",
                "version": "1.0.0",
                "title": "Fixture implementation",
                "stages": ["generate"],
                "dependencies": [],
                "instructions": "Implement the declared public contracts.",
                "limitations": ["Do not invent behavior."],
                "trust": "fixture-reviewed",
            }
        ),
        encoding="utf-8",
    )
    component_document = fixture.component / "component.md"
    document, body = parse_authoring_markdown(
        component_document.read_bytes(), source=str(component_document)
    )
    document["authoring_inputs"].extend(
        {
            "kind": "specification-to-source-skill",
            "uri": f"skills/specification-to-source/{name}/SKILL.md",
        }
        for name in (
            "portable-specification-planning",
            "portable-application-implementation",
        )
    )
    component_document.write_bytes(render_authoring_markdown(document, body))
    (fixture.component / "specs/spec.md").write_text(
        "# Native scale application\n\n"
        "### Requirement: Admitted native scale\n\n"
        "The application SHALL return the vendor.math scale result as a canonical "
        "decimal string and SHALL bind the runtime-provided native SDK metadata.\n\n"
        "#### Scenario: Scale two values\n\n"
        "- **WHEN** the application receives a value and factor\n"
        "- **THEN** it returns their native SDK product\n",
        encoding="utf-8",
    )
    (fixture.component / "acceptance/execution.json").write_text(
        json.dumps(
            {
                "invocations": [{"arguments": [3, 4]}],
                "result_shape": "string",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


class _SpecificationBoundNativeSdkGenerator:
    """Generate one deterministic application through the public model seam."""

    def __init__(self, root_revision: ContentIdentity) -> None:
        self.root_revision = root_revision
        self.calls = 0

    def __call__(self, prepared) -> SourceGenerationRunOutput:
        recipe = prepared.recipe
        if len(recipe.native_sdk_dependencies) != 1:
            raise RuntimeError("native SDK generation requires one admitted dependency")
        root = Path(prepared.workspace.locator)
        source = root / "source"
        tests = source / "tests"
        tests.mkdir(parents=True)
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (source / "main.py").write_text(
            "import json\n"
            "import sys\n"
            "import vendor_math\n"
            "from literate_ai_native_sdk import binding\n\n"
            "def main(value, factor):\n"
            "    authority = binding('vendor_math')\n"
            "    assert authority['sdk_snapshot_identity'].startswith('sha256:')\n"
            "    assert authority['target_identity'].startswith('sha256:')\n"
            "    return format(vendor_math.scale(value, factor), '.15g')\n\n"
            "if __name__ == '__main__':\n"
            "    if sys.argv[1:] == ['--litai-test']:\n"
            "        from tests.litai_test import run\n"
            "        result = run()\n"
            "    elif sys.argv[1:] == ['--litai-smoke']:\n"
            "        result = main(2, 4)\n"
            "    else:\n"
            "        result = main(*json.loads(sys.argv[1]))\n"
            "    print(json.dumps(result, sort_keys=True, separators=(',', ':')))\n",
            encoding="utf-8",
        )
        cases_literal = repr(tuple(case[:4] for case in _SDK_CASES))
        (tests / "litai_test.py").write_text(
            "from main import main\n\n"
            f"CASES = {cases_literal}\n\n"
            "def run():\n"
            "    observed = []\n"
            "    for case_id, value, factor, expected in CASES:\n"
            "        if main(value, factor) != expected:\n"
            "            raise AssertionError(case_id)\n"
            "        observed.append({'case_id': case_id, 'outcome': 'passed'})\n"
            "    return {'schema': 'literate-ai/generated-test-results@1', "
            "'cases': observed}\n",
            encoding="utf-8",
        )
        reference = recipe.non_acceptance_document_paths[0]
        suite_content = json.dumps(
            {
                "schema": GENERATED_TEST_SUITE_SCHEMA,
                "recipe_identity": recipe.identity,
                "generation_mode": MAJOR_REBUILD_GENERATION_MODE,
                "cases": [
                    {
                        "case_id": case_id,
                        "category": category,
                        "specification_refs": [reference],
                        "arguments": [value, factor],
                        "expected_result": expected,
                    }
                    for case_id, value, factor, expected, category in _SDK_CASES
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        suite_path = root.joinpath(*Path(GENERATED_TEST_SUITE_PATH).parts)
        suite_path.parent.mkdir(parents=True, exist_ok=True)
        suite_path.write_bytes(suite_content)
        suite = validate_generated_test_suite(
            suite_content,
            recipe_identity=recipe.identity,
            specification_references=recipe.non_acceptance_document_paths,
            acceptance_arguments=_acceptance_argument_vectors(recipe),
            result_shape=_acceptance_result_shape(recipe),
        )
        authority_components, authority_edges = _recipe_authority_sbom(recipe)
        sdk_components = tuple(
            {
                "type": "library",
                "bom-ref": f"urn:literate-ai:native-sdk:{item.input_identity.digest}",
                "name": item.import_surface.package,
                "version": item.sdk_snapshot_identity.uri,
                "hashes": [
                    {
                        "alg": "SHA-256",
                        "content": item.sdk_snapshot_identity.digest,
                    }
                ],
                "properties": [
                    {"name": "literate-ai:dependency-kind", "value": "runtime"},
                    {"name": "literate-ai:dependency-scope", "value": "runtime"},
                ],
            }
            for item in recipe.native_sdk_dependencies
        )
        source_bom_content, source_bom = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=recipe.managed_sbom_graph,
            additional_components=(*authority_components, *sdk_components),
            additional_edges=(
                *authority_edges,
                *(
                    (
                        recipe.managed_sbom_graph.root_ref,
                        str(item["bom-ref"]),
                    )
                    for item in sdk_components
                ),
            ),
        )
        from literate_ai.contracts import CYCLONEDX_SOURCE_SBOM_PATH

        source_bom_path = root.joinpath(*Path(CYCLONEDX_SOURCE_SBOM_PATH).parts)
        source_bom_path.parent.mkdir(parents=True, exist_ok=True)
        source_bom_path.write_bytes(source_bom_content)
        tree = local_generated_source_tree_identity(root)
        request = prepared.request.request
        recipe_identity = ContentIdentity.parse_uri(recipe.identity)
        planned_request_identity = canonical_identity(
            {
                "schema": "literate-ai/native-sdk-fixture-request@1",
                "prompt_identity": request.prompt_identity.uri,
            }
        )
        candidate = GeneratedSourceCandidate(
            prepared.plan.component_revision,
            request.identity,
            planned_request_identity,
            prepared.plan.identity,
            prepared.plan.generation_key.identity,
            request.context_manifest_identity,
            request.prompt_identity,
            recipe_identity,
            prepared.workspace.allocation_identity,
            tree,
            canonical_identity({"bundle": tree.uri}),
            canonical_identity({"manifest": tree.uri}),
            source_bom.bom_identity,
            ContentIdentity.parse_uri(suite.content_identity),
        )
        provenance = SourceGenerationProvenance(
            request.identity,
            planned_request_identity,
            recipe.component_lock_identity,
            self.root_revision,
            prepared.plan.component_revision,
            prepared.plan.identity,
            prepared.plan.generation_key.identity,
            request.context_manifest_identity,
            request.prompt_identity,
            recipe_identity,
            prepared.workspace.allocation_identity,
            canonical_identity({"readiness": prepared.plan.component_revision.uri}),
            (canonical_identity({"route": "native-sdk-fixture"}),),
            (canonical_identity({"model-output": tree.uri}),),
            candidate.identity,
        )
        self.calls += 1
        return SourceGenerationRunOutput(
            candidate, candidate.identity, provenance, provenance.identity
        )


class NativeSdkAcquisitionTests(unittest.TestCase):
    def test_generated_application_completes_the_public_sdk_lifecycle(self):
        fixture = test_native_sdk_source_build.NativeSdkSourceBuildTests()
        self.addCleanup(fixture.doCleanups)
        fixture.configure_recipe_fixture = _native_sdk_lifecycle_recipe
        fixture.setUp()
        service = fixture.service()
        service.build()
        snapshot = fixture.snapshot
        inputs = NativeSdkConsumerInputs(snapshot=snapshot, services=(service,))
        ids = native_sdk_generation_identities(inputs, snapshot)
        execution = plan_component_execution(
            snapshot.authority.lock,
            model_identities={
                node.revision.identity.uri: canonical_identity(
                    {"model": node.revision.identity.uri}
                )
                for node in snapshot.authority.lock.nodes
            },
            native_sdk_input_identities=ids,
        )
        closure = project_locked_standard_toolchain_closure(
            snapshot, execution, native_sdk_inputs=inputs
        )
        root_node = next(
            node
            for node in snapshot.authority.lock.nodes
            if node.revision.identity == snapshot.authority.lock.root_revision
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            oracle_path = root / "acceptance.json"
            oracle_path.write_text(
                json.dumps(
                    {
                        "schema": "literate-ai/component-acceptance-oracle@1",
                        "specification_set_identity": (
                            root_node.revision.specification_set_identity.uri
                        ),
                        "cases": [
                            {
                                "case_id": "scale-acceptance",
                                "arguments": [3, 4],
                                "expected_result": "12",
                            }
                        ],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            generator = _SpecificationBoundNativeSdkGenerator(
                snapshot.authority.lock.root_revision
            )
            runtime = assemble_filesystem_standard_project_runtime(
                generator=generator,
                object_root=root / "objects",
                toolchain_closure=closure,
                native_sdk_inputs=inputs,
                independent_acceptance_oracle=FilesystemComponentAcceptanceOracle(
                    root,
                    root_node.revision.coordinate.name,
                    path=oracle_path,
                ),
            )
            revisions = tuple(
                plan.component_revision for plan in execution.generation_plans
            )
            invalidation = ComponentInvalidationDecision(
                "native-sdk-production-runtime",
                snapshot.authority.lock.root_revision,
                ComponentChangeSurface.LOCAL_AUTHORITY,
                revisions,
                revisions,
                revisions,
            )
            fixture.fixture.remove_source()
            result = runtime.execute(
                snapshot,
                StandardProjectExecutionRequest(
                    PlannedStandardProject(_selection(), execution),
                    root / "workspaces",
                    invalidation,
                    budget=_budget(),
                ),
            )

        self.assertTrue(
            result.lifecycle.successful,
            (
                tuple(
                    (item.failure_code, item.failure_evidence)
                    for item in result.lifecycle.node_results
                ),
                runtime.lifecycle_ports.failure_diagnostics,
            ),
        )
        self.assertEqual(generator.calls, 1)
        node = result.lifecycle.node_results[0]
        self.assertIsNotNone(node.generated_test_evidence)
        self.assertEqual(node.generated_test_evidence.passed_count, len(_SDK_CASES))
        self.assertIsNotNone(node.execution_evidence)
        self.assertIsNotNone(node.acceptance_evidence)
        self.assertIsNotNone(result.lifecycle.root_integration)
        self.assertIsNotNone(
            result.lifecycle.root_integration.independent_acceptance_identity
        )

    def test_preflight_and_real_acquisition_feed_preparation(self):
        fixture = test_native_sdk_source_build.NativeSdkSourceBuildTests()
        self.addCleanup(fixture.doCleanups)
        fixture.configure_recipe_fixture = preparation_recipe
        fixture.setUp()
        root = fixture.fixture.root / "acquired"
        options = dict(
            cache_root=root,
            source_intelligence_policy=source_intelligence_policy(
                "none", SourceIntelligenceMode.OFF
            ),
            security_policy=SecurityPolicy(canonical_identity("fixture policy").uri),
            revocations=lambda: fixture.revocations,
            environment=dict(os.environ),
            actor="fixture-operator",
            reason="Build the reviewed native acquisition fixture",
            host_build_acknowledged=True,
        )
        with patch("literate_ai.adapters.native_sdk_acquisition.shutil.which") as which:
            with self.assertRaises(GenerationPreparationError) as raised:
                NativeSdkProjectAcquisition(
                    **{**options, "host_build_acknowledged": False}
                )(fixture.snapshot)
            self.assertEqual(
                raised.exception.code, "native_sdk.host_build_not_acknowledged"
            )
            which.assert_not_called()
        self.assertFalse(root.exists())
        with self.assertRaises(GenerationPreparationError) as raised:
            NativeSdkProjectAcquisition(**{**options, "environment": {"PATH": ""}})(
                fixture.snapshot
            )
        self.assertEqual(raised.exception.code, "native_sdk.tool_unavailable")
        self.assertFalse(root.exists())
        with patch(
            "literate_ai.adapters.native_sdk_acquisition.platform.machine",
            return_value="unsupported-target",
        ):
            with self.assertRaises(GenerationPreparationError) as raised:
                NativeSdkProjectAcquisition(**options)(fixture.snapshot)
        self.assertEqual(raised.exception.code, "native_sdk.host_target_mismatch")
        self.assertFalse(root.exists())
        with patch("literate_ai.adapters.native_sdk_acquisition.shutil.which") as which:

            def rejected_revocations():
                raise AuthorizationError("native_sdk.project_authority_changed")

            with self.assertRaises(GenerationPreparationError) as raised:
                NativeSdkProjectAcquisition(
                    **{**options, "revocations": rejected_revocations}
                )(fixture.snapshot)
            self.assertEqual(
                raised.exception.code, "native_sdk.project_authority_changed"
            )
            which.assert_not_called()
        self.assertFalse(root.exists())
        with patch(
            "literate_ai.adapters.native_sdk_acquisition.NativeSdkSourceBuildService",
            side_effect=RepositorySourceCachePolicyError(
                "repository_source.intelligence_unavailable", "Indexer unavailable"
            ),
        ):
            with self.assertRaises(GenerationPreparationError) as raised:
                NativeSdkProjectAcquisition(**options)(fixture.snapshot)
            self.assertEqual(
                raised.exception.code, "repository_source.intelligence_unavailable"
            )
        acquisition = NativeSdkProjectAcquisition(**options)
        adapter = FilesystemLockedGenerationApplicationAdapter(
            FilesystemLockedGenerationPreparationAdapter(native_sdk_builder=acquisition)
        )
        recipe_fixture = fixture.recipe_fixture
        prepared = adapter.prepare(
            GenerationPreparationRequest(
                component_root=recipe_fixture.component,
                target_name="host",
                flavor_selectors=(
                    "+python",
                    "+" + recipe_fixture.recipe.layout.operating_system,
                ),
                flavor_roots=(recipe_fixture.flavors,),
            )
        )
        snapshot = prepared.locked_authority_snapshot
        inputs = prepared.native_sdk_inputs
        self.assertIsNotNone(inputs)
        ids = native_sdk_generation_identities(inputs, snapshot)
        self.assertEqual(len(ids), 1)
        self.assertNotIn(snapshot.authority.lock.root_revision.uri, ids)
        execution = plan_component_execution(
            snapshot.authority.lock,
            model_identities={
                node.revision.identity.uri: canonical_identity("model")
                for node in snapshot.authority.lock.nodes
            },
            native_sdk_input_identities=ids,
        )
        self.assertEqual(len(execution.generation_plans), 2)
        closure = project_locked_standard_toolchain_closure(
            snapshot,
            execution,
            native_sdk_inputs=inputs,
            toolchain_discoverer=lambda name, *_: _tool(name),
            dependency_observer=_observation,
        )
        root_contract = next(
            contract
            for contract in closure.contracts
            if contract.component_revision == snapshot.authority.lock.root_revision
        )
        self.assertIn(
            ComponentCommandRole.NATIVE_SDK_INPUTS,
            root_contract.command(ComponentCommandPhase.EXECUTE).roles,
        )
        adapter.require_unchanged(prepared)
        with self.assertRaisesRegex(ValueError, "single-use"):
            acquisition(snapshot)
        path = recipe_fixture.component / "provider" / "integration.md"
        path.write_bytes(path.read_bytes() + b"\nChanged contract.\n")
        with self.assertRaises(LockedGenerationAuthorityReaderError) as changed:
            adapter.require_unchanged(prepared)
        self.assertIn("stale", str(getattr(changed.exception, "code", "")))
