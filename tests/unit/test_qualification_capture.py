"""Qualification products retain exact accepted bytes without granting admission."""

import hashlib
import json
import subprocess
import unittest
from dataclasses import fields, replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.component_acceptance import (
    DeclaredLibraryAcceptanceCase,
    LibraryAcceptance,
)
from literate_ai.adapters.dependencies import build_cyclonedx_bom
from literate_ai.adapters.models.coding_cli import GenerationRecipe, RecipeDocument
from literate_ai.adapters.qualification import (
    FilesystemStandardQualificationAdapter,
    _observe,
)
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
    capture_lifecycle_records,
    capture_qualification_run,
    reopen_qualification_lifecycle,
    reopen_qualification_products,
    verify_qualification_packaged_library_processes,
)
from literate_ai.application.artifact_graph import (
    create_artifact_build_graph,
    create_package_plan,
)
from literate_ai.application.standard_test_receipts import _aggregate
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.executable_components.packages import PackageKind
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.library_products import LibraryArtifactProduct
from literate_ai.contracts.retained_libraries import RetainedLibraryExportSet
from literate_ai.contracts.sbom import (
    LITERATE_SOURCE_BOM_IDENTITY_PROPERTY,
    CycloneDxLifecycle,
    project_component_lock_managed_graph,
)
from literate_ai.contracts.standard_lifecycle_membership import StandardAggregateReceipt
from literate_ai.contracts.standard_root_integration import (
    StandardRootIntegrationEvidence,
)
from literate_ai.contracts.testing import ProjectTestEvidence
from literate_ai.source_to_specification.qualification_lifecycle import (
    QualificationLifecycleResult,
    QualificationLifecycleRunEvidence,
)
from tests.support import fixtures_test_artifact_graph_contracts as graph_fixtures
from tests.support import fixtures_test_package_release_contracts as package_fixtures
from tests.support import (
    fixtures_test_qualification_lifecycle_runner as lifecycle_fixtures,
)
from tests.support.fixtures_test_library_products import library_product
from tests.support.fixtures_test_standard_post_source_evidence import _evidence


def fixture():
    content = b"exact accepted package bytes"
    acceptance = _evidence()
    old_export = acceptance.build.exports[0]
    export = replace(
        old_export,
        role="library",
        blob=BlobRef(
            hashlib.sha256(content).hexdigest(),
            len(content),
            media_type=old_export.media_type,
        ),
    )
    build = replace(
        acceptance.build,
        exports=(export,),
        resolved_sbom_export_identities=(export.identity,),
    )
    tests = replace(
        acceptance.generated_tests,
        build_evidence_identity=build.identity,
        export_identities=build.export_identities,
    )
    acceptance = replace(
        acceptance,
        build=build,
        generated_tests=tests,
        execution=replace(
            acceptance.execution,
            build_evidence_identity=build.identity,
            export_identities=build.export_identities,
            root_export_identity=export.identity,
        ),
    )
    graph_fixture = graph_fixtures.ArtifactGraphTests()
    graph = create_artifact_build_graph(
        build_system_driver_identity=graph_fixture.driver,
        manifests=(graph_fixture.manifest(export),),
        link_roots=(export.identity,),
    )
    exports = RetainedLibraryExportSet(
        graph,
        graph.link_plans[0].identity,
        (LibraryArtifactProduct(export, library_product("rust").import_surface),),
    )
    identity = canonical_identity
    values = {
        field.name: identity(field.name)
        for field in fields(QualificationLifecycleRunEvidence)
    }
    values.update(
        target_profile_identity=export.target_identity,
        source_tree_identities=(build.source_tree_identity,),
        source_index_identities=(identity("index"),),
        build_evidence_identities=(build.identity,),
        resolved_sbom_identities=(build.resolved_sbom.bom_identity,),
        generated_test_suite_identities=(tests.generated_test_suite_identity,),
        generated_test_evidence_identities=(tests.identity,),
        generated_test_case_identities=tuple(
            case.case_identity for case in tests.cases
        ),
        acceptance_evidence_identities=(acceptance.identity,),
        cache_decision_identities=(identity("cache"),),
        node_workspace_identities=(identity("workspace"),),
        generated_test_total=len(tests.cases),
        covered_surface_ids=("fixture.logic",),
    )
    return QualificationLifecycleRunEvidence(**values), exports, acceptance, content


def root_records(run, exports):
    """Contract fixture for exact root/receipt binding, not native qualification."""

    identity = canonical_identity
    root_export = exports.libraries[0].artifact_export
    plan = create_package_plan(
        exports.graph,
        root_component_revision=root_export.component_revision,
        component_lock_identity=run.component_lock_identity,
        target_identity=run.target_profile_identity,
        root_artifact_identity=root_export.identity,
        package_kind=PackageKind.DIRECTORY,
        packager_identity=identity("packager"),
        destinations={root_export.identity.uri: "library.zip"},
        entrypoints=(),
        runtime_requirements=(),
    )
    package = package_fixtures.PackageReleaseContractTests().result(plan)
    root = StandardRootIntegrationEvidence(
        component_lock_identity=run.component_lock_identity,
        execution_plan_identity=identity("execution"),
        project_build_plan_identity=identity("build-plan"),
        artifact_graph=exports.graph,
        link_plan=exports.link_plan,
        package_plan=plan,
        package_result=package,
        root_generated_integration_test_identity=identity("integration-test"),
        packaged_execution_identity=identity("packaged-execution"),
        independent_acceptance_identity=identity("independent-acceptance"),
    )
    aggregate = StandardAggregateReceipt(
        root.execution_plan_identity,
        identity("membership"),
        (identity("node-result"),),
        identity("admission"),
        (identity("prompt-journal"),),
        (identity("benchmark"),),
        identity("context-cache"),
        root.identity,
    )
    lifecycle = {
        "schema": "literate-ai/standard-project-lifecycle-result@4",
        "execution_plan_identity": root.execution_plan_identity.uri,
        "validation_identity": identity("validation").uri,
        "project_build_plan_identity": root.project_build_plan_identity.uri,
        "generation_schedule_identity": identity("schedule").uri,
        "node_results": [item.uri for item in aggregate.lifecycle_result_identities],
        "lifecycle_membership_identity": aggregate.lifecycle_membership_identity.uri,
        "admission_identity": aggregate.admission_identity.uri,
        "aggregate_receipt_identity": aggregate.identity.uri,
        "receipt_identity": aggregate.identity.uri,
        "root_integration_evidence_identity": root.identity.uri,
        "context_prompt_journal_identities": [
            item.uri for item in aggregate.context_prompt_journal_identities
        ],
        "context_benchmark_record_identities": [
            item.uri for item in aggregate.context_benchmark_record_identities
        ],
        "context_cache_report_identity": aggregate.context_cache_report_identity.uri,
        "candidate_attempt_chain_identities": [],
    }
    receipt = lifecycle_fixtures._receipt(
        SimpleNamespace(identity=identity(lifecycle), aggregate_receipt=aggregate),
        total=run.generated_test_total,
    )
    evidence = {item.kind: item.identity for item in receipt.evidence}
    evidence.update(
        {
            "lifecycle-command": run.lifecycle_invocation_identity,
            "lifecycle-request": run.lifecycle_request_identity,
            "lifecycle-plan": root.execution_plan_identity,
            "source-cache-lifecycle": aggregate.lifecycle_membership_identity,
            "workspace-admission": aggregate.admission_identity,
        }
    )
    for kind, members in (
        ("acceptance-result", run.acceptance_evidence_identities),
        ("build-result", run.build_evidence_identities),
        ("source-intelligence", run.source_index_identities),
        ("resolved-sbom", run.resolved_sbom_identities),
        ("test-report", run.generated_test_evidence_identities),
    ):
        evidence[kind] = _aggregate(kind, members)
    receipt = replace(
        receipt,
        evidence=tuple(
            ProjectTestEvidence(kind, value) for kind, value in sorted(evidence.items())
        ),
    )
    run = replace(
        run,
        lifecycle_result_identity=identity(lifecycle),
        project_receipt_identity=receipt.identity,
        lifecycle_policy_identity=receipt.suite.content_identity,
    )
    return run, (lifecycle, root.to_dict(), aggregate.to_dict(), receipt.to_dict())


class QualificationCaptureTests(unittest.TestCase):
    def adapter_fixture(self):
        run, exports, acceptance, content = fixture()
        lifecycle_fixture = lifecycle_fixtures.QualificationLifecycleRunnerTests()
        lifecycle_fixture.setUp()
        case_map = lifecycle_fixture.case_map
        verifier = lifecycle_fixtures.Verifier([], case_map)
        runs, parity_records, sources, readers, root_documents = [], [], {}, [], []
        for ordinal, run_id in enumerate(lifecycle_fixture.plan.run_identities):
            parity = verifier.verify(
                run_identity=run_id,
                source_snapshot_identity=run.source_snapshot_identity,
                generated_tree_identities=run.source_tree_identities,
                case_map=case_map,
            )
            current = replace(
                run,
                run_identity=run_id,
                workspace_allocation_identity=canonical_identity(
                    ["allocation", ordinal]
                ),
                node_workspace_identities=(canonical_identity(["node", ordinal]),),
                covered_surface_ids=case_map.surface_ids,
                parity_evidence_identity=parity.identity,
            )
            current, documents = root_records(current, exports)
            root_documents.extend(documents)
            runs.append(current)
            parity_records.append(parity)
            reader = Mock(return_value=content)
            readers.append(reader)
            ports = SimpleNamespace(
                project_package_custody=Mock(),
                read_artifact_blob=reader,
                contracts={
                    acceptance.component_revision.uri: SimpleNamespace(
                        library_import_surface=exports.libraries[0].import_surface
                    )
                },
            )
            execution = SimpleNamespace(
                receipt=SimpleNamespace(identity=current.project_receipt_identity),
                lifecycle=SimpleNamespace(
                    identity=current.lifecycle_result_identity,
                    node_results=(SimpleNamespace(acceptance_evidence=acceptance),),
                    root_integration=SimpleNamespace(
                        artifact_graph=exports.graph,
                        link_plan=exports.link_plan,
                        package_plan=object(),
                        package_result=object(),
                    ),
                ),
            )
            sources[run_id.uri] = (execution, ports)
        result = QualificationLifecycleResult(
            tuple(runs), case_map, tuple(parity_records)
        )
        adapter = object.__new__(FilesystemStandardQualificationAdapter)
        adapter.max_capture_bytes = 1_000_000
        adapter.library_captures = (object(),)
        adapter.lifecycle = SimpleNamespace(
            retain_library_products=True,
            product_sources={},
            require_current_authority=Mock(),
        )
        adapter.parity = SimpleNamespace(case_map=case_map)

        def complete(_plan):
            adapter.lifecycle.product_sources.update(sources)
            for document in root_documents:
                adapter.lifecycle.evidence_recorder.remember_json(document)
            return result

        adapter.runner = SimpleNamespace(run=complete)
        return adapter, lifecycle_fixture.plan, result, readers, content

    def library_authority_fixture(self):
        from tests.support.fixtures_test_standard_project_lifecycle import (
            StandardProjectLifecycleTests,
        )

        producer = StandardProjectLifecycleTests()
        producer.setUp()
        surface = library_product("rust").import_surface
        root = next(
            node.revision
            for node in producer.lock.nodes
            if node.revision.identity == producer.lock.root_revision
        )
        harness = b"authored oracle contract fixture; never executed"
        oracle = LibraryAcceptance(
            root.coordinate.name,
            root.specification_set_identity,
            tuple(
                sorted(
                    (item.identity for item in root.public_interfaces),
                    key=lambda item: item.uri,
                )
            ),
            surface.identity,
            surface.language,
            ContentIdentity.parse_uri("sha256:" + hashlib.sha256(harness).hexdigest()),
            harness,
            (
                DeclaredLibraryAcceptanceCase(
                    "known", surface.capabilities[0].capability, [], {"value": True}
                ),
            ),
        )
        self.current_lock, self.current_oracle = producer.lock, oracle
        from literate_ai.contracts.projects import StandardProjectLifecycleDriver
        from literate_ai.contracts.standard_lifecycle_policy import (
            load_current_standard_lifecycle_policy,
        )

        self.current_driver = StandardProjectLifecycleDriver(
            fixture()[0].framework_distribution_identity,
            load_current_standard_lifecycle_policy().identity,
        )
        from tests.support.fixtures_test_standard_project_lifecycle import (
            command_contract_fixture,
        )

        self.current_commands = {
            node.plan.component_revision: command_contract_fixture(
                node.plan.component_revision,
                producer.names[node.plan.component_revision.uri],
                library_exports=True,
            )
            for node in producer.nodes.values()
        }
        from tests.support.fixtures_test_coding_cli_generation import generation_skill

        self.current_recipes = {
            node.plan.component_revision: GenerationRecipe(
                "retained-test-" + node.plan.component_revision.digest,
                "retained-fixture",
                tuple(
                    RecipeDocument.create(path, uri)
                    for path in ("openspec/spec.md", "cpp/openspec/spec.md")
                ),
                producer.lock.identity,
                skills=(generation_skill(),),
                managed_sbom_graph=project_component_lock_managed_graph(
                    producer.lock, node.plan.component_revision
                ),
            )
            for uri, node in producer.nodes.items()
        }
        from literate_ai.source_to_specification.host_qualification import (
            LocalQualificationCase,
            LocalQualificationProfile,
        )

        self.current_profile = LocalQualificationProfile(
            "retained-parity-fixture@1",
            ("fixture-build",),
            (("fixture-test",),),
            ("fixture-baseline",),
            ("fixture-generated", "{workspace}", "{build_root}"),
            tuple(
                LocalQualificationCase(
                    case_id, ({"value": ordinal},), {"value": ordinal}
                )
                for ordinal, case_id in enumerate(("case-errors", "case-output"))
            ),
            ("cli", "errors", "output"),
            maximum_output_bytes=512,
        )
        return producer, surface, oracle

    def typed_product_fixture(self):
        producer, surface, oracle = self.library_authority_fixture()
        qualification = lifecycle_fixtures.QualificationLifecycleRunnerTests()
        qualification.setUp()
        import tempfile
        from pathlib import Path

        from literate_ai.source_to_specification.contracts import canonical_value
        from literate_ai.source_to_specification.inventory import inventory_source
        from literate_ai.source_to_specification.qualification_lifecycle import (
            QualificationCaseSurfaceBinding,
            QualificationParityCaseEvidence,
            QualificationParityEvidence,
            QualificationVerifierCaseMap,
        )

        recorder = QualificationEvidenceRecorder(max_bytes=5_000_000, max_records=1000)
        with tempfile.TemporaryDirectory() as baseline:
            (Path(baseline) / "library.rs").write_text(
                "pub fn fixture() {}\n", encoding="utf-8"
            )
            source_inventory = inventory_source(baseline)
        baseline_identity = recorder.remember_json(canonical_value(source_inventory))
        self.assertEqual(baseline_identity.uri, source_inventory.identity)
        recorder.remember_json(oracle.identity_document())
        recorder.remember_bytes(oracle.harness_content)
        recorder.remember_json(surface.to_dict())

        def accept_root(
            lock, execution_plan, build_plan, plan, package, root_test, execution
        ):
            observations = []
            for case in oracle.cases:
                case_identity = recorder.remember_json(
                    {
                        "arguments": case.arguments,
                        "expected_result": case.expected_result,
                    }
                )
                result_identity = recorder.remember_json(case.expected_result)
                observations.append(
                    {
                        "case_id": case.case_id,
                        "capability": case.capability,
                        "case_identity": case_identity.uri,
                        "result_identity": result_identity.uri,
                    }
                )
            return recorder.remember_json(
                {
                    "schema": "literate-ai/local-independent-library-acceptance@1",
                    "package_plan_identity": plan.identity.uri,
                    "package_result_identity": package.identity.uri,
                    "root_integration_test_identity": root_test.uri,
                    "packaged_execution_identity": execution.uri,
                    "oracle_identity": oracle.identity.uri,
                    "harness_identity": oracle.harness_identity.uri,
                    "artifact_identity": plan.root_artifact_identity.uri,
                    "import_surface_identity": surface.identity.uri,
                    "observations": observations,
                }
            )

        from tests.support.fixtures_test_generated_tests import valid_suite
        from tests.support.fixtures_test_standard_project_lifecycle import _Recipe

        generated_suites = {}
        for uri, node in producer.nodes.items():
            recipe = self.current_recipes[node.plan.component_revision]
            producer.nodes[uri] = replace(
                node,
                recipe=_Recipe(
                    ContentIdentity.parse_uri(recipe.identity),
                    producer.lock.identity,
                ),
            )
            suite = valid_suite()
            suite["recipe_identity"] = recipe.identity
            for case in suite["cases"]:
                case["case_id"] += "-" + node.plan.component_revision.digest[:12]
            generated_suites[node.plan.component_revision] = json.dumps(suite).encode()

        generated_boms = {}
        for revision, recipe in self.current_recipes.items():
            graph = recipe.managed_sbom_graph
            source, _ = build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=graph
            )
            resolved, _ = build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.RESOLVED,
                managed_graph=graph,
                source_bom=source,
                source_managed_graph=graph,
            )
            generated_boms[revision] = (source, resolved, graph)

        runs, parities, captures = [], [], []
        for ordinal, run_id in enumerate(qualification.plan.run_identities):
            producer.nodes = {
                uri: replace(
                    node,
                    workspace=replace(
                        node.workspace,
                        allocation_identity=canonical_identity(
                            ["workspace", ordinal, uri]
                        ),
                        locator=f"fixture://clean-{ordinal}/{uri}",
                    ),
                )
                for uri, node in producer.nodes.items()
            }
            run_suites = {}
            for revision, content in generated_suites.items():
                document = json.loads(content)
                for case in document["cases"]:
                    case["case_id"] += f"-run{ordinal}"
                run_suites[revision] = json.dumps(document).encode()
            run_boms = {}
            for revision, (source, resolved, graph) in generated_boms.items():
                # Distinct descriptive text keeps the exact run documents
                # separate without changing current dependency authority.
                document = json.loads(source)
                document["metadata"]["component"]["description"] = (
                    f"Contract fixture for clean run {ordinal}"
                )
                source = canonical_json_bytes(document)
                resolved_document = json.loads(resolved)
                resolved_document["metadata"]["component"]["description"] = document[
                    "metadata"
                ]["component"]["description"]
                for prop in resolved_document["metadata"]["properties"]:
                    if prop["name"] == LITERATE_SOURCE_BOM_IDENTITY_PROPERTY:
                        prop["value"] = "sha256:" + hashlib.sha256(source).hexdigest()
                resolved = canonical_json_bytes(resolved_document)
                run_boms[revision] = (source, resolved, graph)
            _, run, execution = producer.captured_qualification_run_fixture(
                library_exports=True,
                root_acceptance=accept_root,
                recorder=recorder,
                generated_suites=run_suites,
                generated_boms=run_boms,
            )
            run = replace(run, source_snapshot_identity=baseline_identity)
            profile = self.current_profile
            provider = recorder.remember_json(
                {
                    "provider": "filesystem-standard-independent-parity@1",
                    "source_snapshot_identity": run.source_snapshot_identity.uri,
                    "profile_identity": profile.identity,
                }
            )
            recorder.remember_json(profile.to_dict())
            qualification.case_map = QualificationVerifierCaseMap(
                provider,
                tuple(
                    sorted(
                        (
                            QualificationCaseSurfaceBinding(
                                case.case_id,
                                recorder.remember_json(case.to_dict()),
                                profile.covered_surface_ids,
                            )
                            for case in profile.cases
                        ),
                        key=lambda item: (item.case_id, item.case_identity.uri),
                    )
                ),
            )
            recorder.remember_json(qualification.case_map.to_dict())
            parity_cases = []
            for case in profile.cases:
                observation_ids = []
                argument = json.dumps(
                    case.arguments,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                for command in (profile.source_command, profile.generated_command):
                    with patch(
                        "literate_ai.adapters.qualification.run_with_tree_kill",
                        return_value=subprocess.CompletedProcess(
                            command, 0, canonical_json_bytes(case.expected_result), b""
                        ),
                    ):
                        _, observation, _ = _observe(
                            (*command, argument),
                            cwd=None,
                            environment={},
                            timeout_seconds=profile.timeout_seconds,
                            maximum_output_bytes=profile.maximum_output_bytes,
                            recorder=recorder,
                        )
                    observation_ids.append(observation)
                evidence = QualificationParityCaseEvidence(
                    case.case_id,
                    canonical_identity(case.to_dict()),
                    *observation_ids,
                    True,
                )
                recorder.remember_json(evidence.to_dict())
                parity_cases.append(evidence)
            parity = QualificationParityEvidence(
                run_id,
                run.source_snapshot_identity,
                run.source_tree_identities,
                provider,
                qualification.case_map.identity,
                tuple(parity_cases),
            )
            recorder.remember_json(parity.to_dict())
            run = replace(
                run,
                run_identity=run_id,
                workspace_allocation_identity=canonical_identity(
                    ["allocation", ordinal]
                ),
                covered_surface_ids=qualification.case_map.surface_ids,
                parity_evidence_identity=parity.identity,
            )
            lifecycle = execution.lifecycle
            capture_lifecycle_records(lifecycle, recorder)
            recorder.remember_json(execution.receipt.to_dict())
            root = lifecycle.root_integration
            products = tuple(
                sorted(
                    (
                        LibraryArtifactProduct(
                            export, library_product("rust").import_surface
                        )
                        for manifest in root.artifact_graph.manifests
                        for export in manifest.exports
                    ),
                    key=lambda item: item.artifact_export.identity.uri,
                )
            )
            exports = RetainedLibraryExportSet(
                root.artifact_graph, root.link_plan.identity, products
            )
            recorder.remember_json(exports.to_dict())
            acceptances = tuple(
                sorted(
                    (node.acceptance_evidence for node in lifecycle.node_results),
                    key=lambda item: item.identity.uri,
                )
            )
            for acceptance in acceptances:
                recorder.remember_json(acceptance.to_dict())
            custody = {
                product.artifact_export.blob: producer.names[
                    product.artifact_export.component_revision.uri
                ].encode()
                for product in products
            }
            capture = capture_qualification_run(
                run, exports, acceptances, read_blob=custody.__getitem__, max_bytes=4096
            )
            for _, payload in capture.blobs:
                recorder.remember_bytes(payload)
            custody.clear()
            runs.append(run)
            parities.append(parity)
            captures.append(capture)
        result = QualificationLifecycleResult(
            tuple(runs), qualification.case_map, tuple(parities)
        )
        recorder.remember_json(result.to_dict())
        return result, captures, recorder.entries

    def test_real_packaged_process_recorder_reopens_after_cleanup(self):
        import subprocess
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from literate_ai.adapters.lifecycle.standard_local import (
            LocalSourceTreeRegistry,
            LocalStandardLifecyclePorts,
        )

        _, captures, entries = self.typed_product_fixture()
        reader = QualificationEvidenceReader(
            entries, max_bytes=5_000_000, max_records=1000
        )
        lifecycle = reopen_qualification_lifecycle(
            reader, captures[0].run.lifecycle_result_identity
        )
        root = lifecycle.root_integration
        tests = next(
            node.generated_test_evidence
            for node in lifecycle.node_results
            if node.component_revision == root.package_plan.root_component_revision
        )
        case_ids = tuple(case.case_id for case in tests.cases)
        stdout = json.dumps(
            {
                "schema": "literate-ai/generated-test-results@1",
                "cases": [{"case_id": name, "outcome": "passed"} for name in case_ids],
            }
        )
        recorder = QualificationEvidenceRecorder(max_bytes=100_000, max_records=100)
        with tempfile.TemporaryDirectory() as temporary:
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=Path(temporary),
                contracts=(),
            )
            ports.retain_evidence_with(recorder)
            arguments = (
                self.current_lock,
                None,
                None,
                root.package_plan,
                root.package_result,
            )
            with (
                patch.object(
                    ports,
                    "project_package_custody",
                    return_value=SimpleNamespace(
                        generated_test_suite=SimpleNamespace(case_ids=case_ids)
                    ),
                ),
                patch.object(
                    ports,
                    "_run_packaged",
                    side_effect=[
                        subprocess.CompletedProcess(("fixture",), 0, stdout, ""),
                        subprocess.CompletedProcess(
                            ("fixture",), 0, "fixture smoke output", ""
                        ),
                    ],
                ),
            ):
                test_id = ports.test_root_integration(*arguments)
                execution_id = ports.execute_packaged_project(*arguments)
        self.assertFalse(Path(temporary).exists())
        verify_qualification_packaged_library_processes(
            QualificationEvidenceReader(
                recorder.entries, max_bytes=100_000, max_records=100
            ),
            root=replace(
                root,
                root_generated_integration_test_identity=test_id,
                packaged_execution_identity=execution_id,
            ),
            tests=tests,
        )

    def test_rehashed_parity_observations_cannot_preserve_a_false_pass(self):
        result, captures, entries = self.typed_product_fixture()
        original = QualificationEvidenceReader(
            entries, max_bytes=5_000_000, max_records=1000
        )
        parity = result.parity_evidence[1]
        case = parity.cases[0]
        for field, value, reason in (
            ("exit_status", 1, "parity-record-mismatch"),
            ("json_output_valid", False, "parity-record-mismatch"),
            ("stdout_bytes", b'{"value":false}', "parity-result-mismatch"),
            ("stdout_bytes", b"NaN", "parity-record-invalid"),
            ("stdout_bytes", b"x" * 513, "parity-output-limit"),
        ):
            recorder = QualificationEvidenceRecorder(
                max_bytes=5_000_000, max_records=1000
            )
            for _, payload in entries:
                recorder.remember_bytes(payload)
            document = original.read_json(case.generated_observation_identity)
            if field.endswith("_bytes"):
                document[field.removesuffix("_bytes")] = recorder.remember_bytes(
                    value
                ).uri
            else:
                document[field] = value
            changed_case = replace(
                case, generated_observation_identity=recorder.remember_json(document)
            )
            recorder.remember_json(changed_case.to_dict())
            changed_parity = replace(parity, cases=(changed_case, *parity.cases[1:]))
            recorder.remember_json(changed_parity.to_dict())
            changed_run = replace(
                result.runs[1], parity_evidence_identity=changed_parity.identity
            )
            recorder.remember_json(changed_run.to_dict())
            changed_result = replace(
                result,
                runs=(result.runs[0], changed_run),
                parity_evidence=(result.parity_evidence[0], changed_parity),
            )
            recorder.remember_json(changed_result.to_dict())
            with self.subTest(field=field, value=value):
                self.assert_product_records_refused(
                    changed_result, captures, recorder.entries, reason
                )

    def assert_product_records_refused(self, result, captures, entries, reason):
        reader = QualificationEvidenceReader(
            entries, max_bytes=5_000_000, max_records=1000
        )
        with (
            patch.object(reader, "read_bytes", wraps=reader.read_bytes) as reads,
            self.assertRaisesRegex(QualificationCaptureError, reason),
        ):
            reopen_qualification_products(
                reader,
                qualification_identity=result.identity,
                run_identity=captures[0].run.run_identity,
                exports_identity=captures[0].exports.identity,
                component_lock=self.current_lock,
                oracle=self.current_oracle,
                current_recipes=self.current_recipes,
                current_commands=self.current_commands,
                current_profile=self.current_profile,
                current_driver=self.current_driver,
                max_package_bytes=4096,
            )
        package_ids = {
            ContentIdentity.parse_uri(ref.identity)
            for capture in captures
            for ref, _ in capture.blobs
        }
        self.assertTrue(
            package_ids.isdisjoint(call.args[0] for call in reads.call_args_list)
        )

    def test_authority_drift_during_final_package_read_exposes_no_capture(self):
        adapter, plan, _, readers, content = self.adapter_fixture()

        def read_after_drift(_reference):
            adapter.lifecycle.require_current_authority.side_effect = RuntimeError(
                "authority changed during capture"
            )
            return content

        readers[-1].side_effect = read_after_drift
        with self.assertRaisesRegex(RuntimeError, "authority changed during capture"):
            adapter.qualify(plan)
        for reader in readers:
            reader.assert_called_once()
        self.assertEqual(adapter.library_captures, ())
        self.assertEqual(adapter.evidence_blobs, ())
        self.assertEqual(adapter.lifecycle.product_sources, {})
        self.assertIsNone(adapter.lifecycle.evidence_recorder)

    def test_changed_or_mutable_package_bytes_refuse(self):
        run, exports, acceptance, content = fixture()
        for changed in (content[:-1], b"x" * len(content), bytearray(content)):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(QualificationCaptureError, "blob-mismatch"):
                    capture_qualification_run(
                        run,
                        exports,
                        (acceptance,),
                        read_blob=lambda _reference, value=changed: value,
                        max_bytes=1024,
                    )


if __name__ == "__main__":
    unittest.main()
