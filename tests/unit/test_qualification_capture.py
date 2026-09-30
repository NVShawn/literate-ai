"""Qualification products retain exact accepted bytes without granting admission."""

import hashlib
import json
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.component_acceptance import (
    DeclaredLibraryAcceptanceCase,
    LibraryAcceptance,
)
from literate_ai.adapters.dependencies import build_cyclonedx_bom
from literate_ai.adapters.evidence_storage import FileSystemEvidenceStore
from literate_ai.adapters.lifecycle.standard_local import (
    LocalStandardLifecycleError,
    LocalStandardLifecyclePorts,
)
from literate_ai.adapters.models.coding_cli import GenerationRecipe, RecipeDocument
from literate_ai.adapters.qualification import (
    FilesystemStandardQualificationAdapter,
    _observe,
)
from literate_ai.adapters.qualification_archive import (
    encode_qualification_archive,
    reopen_qualification_archive,
)
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
    capture_lifecycle_records,
    capture_qualification_run,
    reopen_qualification_lifecycle,
    reopen_qualification_products,
    reopen_qualification_root,
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
from tests.unit import test_artifact_graph_contracts as graph_fixtures
from tests.unit import test_package_release_contracts as package_fixtures
from tests.unit import test_qualification_lifecycle_runner as lifecycle_fixtures
from tests.unit.test_library_products import library_product
from tests.unit.test_standard_post_source_evidence import _evidence


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
    def test_parity_retains_raw_outputs_and_template_bound_observation(self):
        process = subprocess.CompletedProcess(
            ["fixture"], 0, b'{"value":7}', b"diagnostic"
        )
        recorder = QualificationEvidenceRecorder(max_bytes=4096, max_records=10)
        arguments = dict(
            cwd=None,
            environment={},
            timeout_seconds=1,
            maximum_output_bytes=100,
            identity_command=("python", "{workspace}/probe.py"),
        )
        with patch(
            "literate_ai.adapters.qualification.run_with_tree_kill",
            return_value=process,
        ):
            original = _observe(("python", "/private/run/probe.py"), **arguments)
            captured = _observe(
                ("python", "/private/run/probe.py"), recorder=recorder, **arguments
            )
        self.assertEqual(captured, original)
        self.assertEqual(captured[0], {"value": 7})
        self.assertTrue(captured[2])
        records = dict(recorder.entries)
        document = json.loads(records[captured[1]])
        self.assertEqual(document["command"], ["python", "{workspace}/probe.py"])
        self.assertEqual(
            records[ContentIdentity.parse_uri(document["stdout"])], process.stdout
        )
        self.assertEqual(
            records[ContentIdentity.parse_uri(document["stderr"])], process.stderr
        )

    def test_invalid_parity_json_is_retained_as_failed_observation(self):
        for output in (
            b'{"value":1,"value":2}',
            b"NaN",
            b"Infinity",
            b"1e9999",
            b"\xff",
        ):
            recorder = QualificationEvidenceRecorder(max_bytes=4096, max_records=10)
            with patch(
                "literate_ai.adapters.qualification.run_with_tree_kill",
                return_value=subprocess.CompletedProcess(("fixture",), 0, output, b""),
            ):
                value, identity, valid = _observe(
                    ("fixture",),
                    cwd=None,
                    environment={},
                    timeout_seconds=1,
                    maximum_output_bytes=100,
                    recorder=recorder,
                )
            with self.subTest(output=output):
                self.assertIsNone(value)
                self.assertFalse(valid)
                reader = QualificationEvidenceReader(
                    recorder.entries, max_bytes=4096, max_records=10
                )
                record = reader.read_json(identity)
                self.assertFalse(record["json_output_valid"])
                self.assertEqual(
                    reader.read_bytes(ContentIdentity.parse_uri(record["stdout"])),
                    output,
                )

    def test_process_payloads_reopen_under_unchanged_identities(self):
        ports = object.__new__(LocalStandardLifecyclePorts)
        ports._evidence_recorder = None
        ports._build_evidence = {}
        ports._build_observations = {}
        process = subprocess.CompletedProcess(["fixture"], 0, "actual output", "")
        plan = canonical_identity("plan")
        original = ports._process_observation(process, phase="test", plan_identity=plan)
        recorder = QualificationEvidenceRecorder(max_bytes=4096, max_records=10)
        ports.retain_evidence_with(recorder)
        retained = ports._process_observation(process, phase="test", plan_identity=plan)
        self.assertEqual(retained, original)
        records = dict(recorder.entries)
        observation = json.loads(records[retained])
        self.assertEqual(
            observation["stdout_identity"], canonical_identity(process.stdout).uri
        )
        self.assertEqual(
            json.loads(records[canonical_identity(process.stdout)]), process.stdout
        )
        self.assertEqual(json.loads(records[canonical_identity(process.stderr)]), "")
        ports._build_observations["started"] = plan
        with self.assertRaisesRegex(
            LocalStandardLifecycleError, "capture must precede"
        ):
            ports.retain_evidence_with(recorder)

    def test_recorder_deduplicates_and_freezes_mutable_json_inputs(self):
        recorder = QualificationEvidenceRecorder(max_bytes=64, max_records=1)
        value = {"result": 7}
        identity = recorder.remember_json(value)
        retained_bytes = recorder.retained_bytes
        self.assertEqual(recorder.remember_json(value), identity)
        self.assertEqual(recorder.retained_bytes, retained_bytes)
        value["result"] = 8
        with self.assertRaisesRegex(QualificationCaptureError, "record-limit"):
            recorder.remember_json(value)
        self.assertEqual(json.loads(dict(recorder.entries)[identity]), {"result": 7})

    def test_recorder_byte_limit_does_not_publish_rejected_payload(self):
        recorder = QualificationEvidenceRecorder(max_bytes=3, max_records=2)
        identity = recorder.remember_bytes(b"abc")
        self.assertEqual(recorder.remember_bytes(b"abc"), identity)
        with self.assertRaisesRegex(QualificationCaptureError, "byte-limit"):
            recorder.remember_bytes(b"d")
        self.assertEqual(recorder.entries, ((identity, b"abc"),))
        self.assertEqual(recorder.retained_bytes, 3)

    def test_parallel_producers_share_atomic_bounds_and_deduplication(self):
        for max_bytes, max_records, error in (
            (7, 32, "byte-limit"),
            (32, 7, "record-limit"),
        ):
            with self.subTest(error=error):
                recorder = QualificationEvidenceRecorder(
                    max_bytes=max_bytes, max_records=max_records
                )
                barrier = Barrier(32)

                def remember(index, barrier=barrier, recorder=recorder):
                    barrier.wait(timeout=10)
                    try:
                        recorder.remember_bytes(bytes([index]))
                    except QualificationCaptureError as exc:
                        return str(exc)
                    return "retained"

                with ThreadPoolExecutor(max_workers=32) as executor:
                    results = list(executor.map(remember, range(32)))
                self.assertEqual(results.count("retained"), 7)
                self.assertEqual(results.count("qualification.capture." + error), 25)
                self.assertEqual(recorder.retained_bytes, 7)
                self.assertEqual(len(recorder.entries), 7)
                retained = recorder.entries[0][1]
                with ThreadPoolExecutor(max_workers=8) as executor:
                    duplicates = list(
                        executor.map(recorder.remember_bytes, [retained] * 32)
                    )
                self.assertEqual(len(set(duplicates)), 1)
                self.assertEqual(recorder.retained_bytes, 7)
                self.assertEqual(len(recorder.entries), 7)

    def test_reader_reopens_fresh_json_and_refuses_missing_records(self):
        recorder = QualificationEvidenceRecorder(max_bytes=4096, max_records=4)
        identity = recorder.remember_json({"evidence": [1, 2]})
        raw_identity = recorder.remember_bytes(b"raw harness")
        reader = QualificationEvidenceReader(
            recorder.entries, max_bytes=4096, max_records=4
        )
        document = reader.read_json(identity)
        document["evidence"].append(3)
        self.assertEqual(reader.read_json(identity), {"evidence": [1, 2]})
        self.assertEqual(reader.read_bytes(raw_identity), b"raw harness")
        with self.assertRaisesRegex(QualificationCaptureError, "record-missing"):
            reader.read_json(canonical_identity("missing"))

    def test_reader_refuses_substitution_duplicate_and_oversized_records(self):
        recorder = QualificationEvidenceRecorder(max_bytes=4096, max_records=4)
        identity = recorder.remember_bytes(b"original")
        for entries, limit in (
            (((identity, b"replaced"),), 4096),
            (recorder.entries * 2, 4096),
            (((identity, bytearray(b"original")),), 4096),
            (recorder.entries, 7),
        ):
            with (
                self.subTest(entries=entries),
                self.assertRaises(QualificationCaptureError),
            ):
                QualificationEvidenceReader(entries, max_bytes=limit, max_records=4)

    def test_reader_rejects_noncanonical_json_under_its_correct_byte_digest(self):
        for payload in (b'{"x":1,"x":2}', b'{ "x": 1 }', b"NaN", b'"\xff"'):
            recorder = QualificationEvidenceRecorder(max_bytes=4096, max_records=4)
            identity = recorder.remember_bytes(payload)
            reader = QualificationEvidenceReader(
                recorder.entries, max_bytes=4096, max_records=4
            )
            with (
                self.subTest(payload=payload),
                self.assertRaises(QualificationCaptureError),
            ):
                reader.read_json(identity)

    def adapter_fixture(self, *, corrupt_second=False, byte_limit=1_000_000):
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
            reader = Mock(
                return_value=b"wrong" if corrupt_second and ordinal == 1 else content
            )
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
        adapter.max_capture_bytes = byte_limit
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

    def test_adapter_retains_all_runs_and_releases_runtime_references(self):
        adapter, plan, result, readers, _ = self.adapter_fixture()
        self.assertEqual(adapter.qualify(plan), result)
        self.assertEqual(len(adapter.library_captures), 2)
        self.assertEqual(adapter.lifecycle.product_sources, {})
        records = dict(adapter.evidence_blobs)
        archive = encode_qualification_archive(
            adapter.evidence_blobs, max_bytes=2_000_000, max_records=100_000
        )
        evidence_reader = reopen_qualification_archive(
            archive,
            BlobRef(hashlib.sha256(archive).hexdigest(), len(archive)),
            max_bytes=2_000_000,
            max_records=100_000,
        )
        self.assertEqual(
            QualificationLifecycleResult.from_dict(
                evidence_reader.read_json(result.identity)
            ),
            result,
        )
        for record in (result.case_map, *result.runs, *result.parity_evidence):
            self.assertEqual(
                canonical_identity(json.loads(records[record.identity])),
                record.identity,
            )
        for captured in adapter.library_captures:
            for reference, payload in captured.blobs:
                self.assertEqual(
                    evidence_reader.read_bytes(
                        ContentIdentity.parse_uri(reference.identity)
                    ),
                    payload,
                )
            self.assertEqual(
                RetainedLibraryExportSet.from_dict(
                    json.loads(records[captured.exports.identity])
                ),
                captured.exports,
            )
            for product in captured.exports.libraries:
                self.assertEqual(
                    json.loads(records[product.import_surface.identity]),
                    product.import_surface.to_dict(),
                )
        for reader in readers:
            reader.assert_called_once()

    def library_authority_fixture(self, *, bind_current_target=False):
        from tests.unit.test_standard_project_lifecycle import (
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
        from tests.unit.test_standard_project_lifecycle import command_contract_fixture

        self.current_commands = {
            node.plan.component_revision: command_contract_fixture(
                node.plan.component_revision,
                producer.names[node.plan.component_revision.uri],
                library_exports=True,
                artifact_target_identity=producer.lock.target_profile_identity
                if bind_current_target
                else None,
            )
            for node in producer.nodes.values()
        }
        from tests.unit.test_coding_cli_generation import generation_skill

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

    def typed_product_fixture(
        self,
        *,
        corrupt_second_oracle=False,
        foreign_candidate_recipe=False,
        promotion_audit=None,
        artifact_payloads=None,
    ):
        producer, surface, oracle = self.library_authority_fixture(
            bind_current_target=promotion_audit is not None
        )
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
            if corrupt_second_oracle and ordinal == 1:
                observations[0]["result_identity"] = recorder.remember_json(
                    "foreign-result"
                ).uri
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

        from tests.unit.test_generated_tests import valid_suite
        from tests.unit.test_standard_project_lifecycle import _Recipe

        generated_suites = {}
        for uri, node in producer.nodes.items():
            recipe = self.current_recipes[node.plan.component_revision]
            producer.nodes[uri] = replace(
                node,
                recipe=_Recipe(
                    canonical_identity("foreign-recipe")
                    if foreign_candidate_recipe
                    else ContentIdentity.parse_uri(recipe.identity),
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
                artifact_target_identity=producer.lock.target_profile_identity
                if promotion_audit is not None
                else None,
                artifact_payloads=artifact_payloads,
            )
            run = replace(run, source_snapshot_identity=baseline_identity)
            if promotion_audit is not None:
                current_root = next(
                    n.revision
                    for n in producer.lock.nodes
                    if n.revision.identity == producer.lock.root_revision
                )
                run = replace(
                    run,
                    specification_set_identity=current_root.specification_set_identity,
                    generation_input_audit_identity=ContentIdentity.parse_uri(
                        promotion_audit.identity
                    ),
                    promotion_tree_identity=ContentIdentity.parse_uri(
                        promotion_audit.materialized_tree_identity
                    ),
                )
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
                product.artifact_export.blob: (artifact_payloads or {}).get(
                    producer.names[product.artifact_export.component_revision.uri],
                    producer.names[
                        product.artifact_export.component_revision.uri
                    ].encode(),
                )
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

    def test_disabled_index_capture_reopens_both_products_and_refuses_bad_records(self):
        from literate_ai.adapters.intelligence import DisabledGenerationIndexer
        from tests.unit.test_standard_project_lifecycle import (
            ContractEvidenceLifecyclePorts,
            LifecyclePorts,
        )

        def index(ports, revision, source):
            LifecyclePorts.index(ports, revision, source)
            resolver = Mock()
            indexer = DisabledGenerationIndexer(resolver)
            indexer.retain_evidence_with(ports.process_records)
            identity = indexer.index(revision, source)
            resolver.resolve.assert_called_once_with(source)
            return identity

        with patch.object(ContractEvidenceLifecyclePorts, "index", index):
            result, captures, entries = self.typed_product_fixture()
        reader = QualificationEvidenceReader(
            entries, max_bytes=5_000_000, max_records=1000
        )
        for capture in captures:
            self.assertEqual(
                reopen_qualification_products(
                    reader,
                    qualification_identity=result.identity,
                    run_identity=capture.run.run_identity,
                    exports_identity=capture.exports.identity,
                    max_package_bytes=4096,
                    component_lock=self.current_lock,
                    oracle=self.current_oracle,
                    current_recipes=self.current_recipes,
                    current_commands=self.current_commands,
                    current_profile=self.current_profile,
                    current_driver=self.current_driver,
                ),
                capture,
            )
        missing = captures[0].run.source_index_identities[0]
        self.assert_product_records_refused(
            result,
            captures,
            tuple(e for e in entries if e[0] != missing),
            "record-missing",
        )
        remember = QualificationEvidenceRecorder.remember_json
        for change in (
            {"source": canonical_identity("foreign-source").uri},
            {"component_revision": canonical_identity("foreign-component").uri},
            {"schema": "literate-ai/disabled-source-index@99"},
            {"extra": True},
        ):

            def altered(recorder, document, change=change):
                if isinstance(document, dict) and document.get("schema") == (
                    "literate-ai/disabled-source-index@1"
                ):
                    document = {**document, **change}
                return remember(recorder, document)

            with (
                self.subTest(change=change),
                patch.object(ContractEvidenceLifecyclePorts, "index", index),
                patch.object(QualificationEvidenceRecorder, "remember_json", altered),
            ):
                result, captures, entries = self.typed_product_fixture()
                self.assert_product_records_refused(
                    result, captures, entries, "index-mismatch"
                )

    def test_reopen_exact_products_after_runtime_custody_is_released(self):
        result, captures, entries = self.typed_product_fixture()
        expected = captures[0]
        archive = encode_qualification_archive(
            entries, max_bytes=5_000_000, max_records=1000
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "evidence"
            writer = FileSystemEvidenceStore(root, writable=True)
            reference = writer.put_bytes(archive, media_type="application/zip")
            self.assertEqual(
                writer.put_bytes(archive, media_type="application/zip"), reference
            )
            del archive, writer
            store = FileSystemEvidenceStore(root)
            reader = reopen_qualification_archive(
                store.get_bytes(reference),
                reference,
                max_bytes=5_000_000,
                max_records=1000,
            )
        # Both source scratch and the transport store are gone. Qualification
        # uses only reopened immutable bytes and independently supplied authority.
        arguments = dict(
            qualification_identity=result.identity,
            run_identity=expected.run.run_identity,
            exports_identity=expected.exports.identity,
            max_package_bytes=4096,
            component_lock=self.current_lock,
            oracle=self.current_oracle,
            current_recipes=self.current_recipes,
            current_commands=self.current_commands,
            current_profile=self.current_profile,
            current_driver=self.current_driver,
        )
        for capture in captures:
            self.assertEqual(
                reopen_qualification_products(
                    reader,
                    **{
                        **arguments,
                        "run_identity": capture.run.run_identity,
                        "exports_identity": capture.exports.identity,
                    },
                ),
                capture,
            )
        with self.assertRaisesRegex(QualificationCaptureError, "run-missing"):
            reopen_qualification_products(
                reader,
                **{**arguments, "run_identity": canonical_identity("foreign-run")},
            )
        for missing in (
            result.identity,
            expected.run.lifecycle_result_identity,
            expected.run.project_receipt_identity,
            ContentIdentity.parse_uri(
                reader.read_json(expected.run.lifecycle_result_identity)[
                    "root_integration_evidence_identity"
                ]
            ),
            expected.acceptances[0].identity,
            self.current_oracle.identity,
            self.current_oracle.harness_identity,
            canonical_identity(
                {
                    "arguments": self.current_oracle.cases[0].arguments,
                    "expected_result": self.current_oracle.cases[0].expected_result,
                }
            ),
            canonical_identity(self.current_oracle.cases[0].expected_result),
            *(
                reopen_qualification_root(
                    reader, capture.run
                ).independent_acceptance_identity
                for capture in captures
            ),
            ContentIdentity.parse_uri(expected.blobs[0][0].identity),
        ):
            incomplete = QualificationEvidenceReader(
                tuple(item for item in entries if item[0] != missing),
                max_bytes=5_000_000,
                max_records=1000,
            )
            with (
                self.subTest(missing=missing),
                self.assertRaisesRegex(QualificationCaptureError, "record-missing"),
            ):
                reopen_qualification_products(incomplete, **arguments)

    def test_rehashed_packaged_process_records_refuse_before_package_reads(self):
        original = QualificationEvidenceRecorder.remember_json
        scenarios = [
            (phase, field, value)
            for phase in ("packaged-root-generated-test", "packaged-project-execution")
            for field, value in (
                ("returncode", 1),
                ("returncode", True),
                ("plan_identity", canonical_identity("foreign-package-plan").uri),
                ("phase", "foreign-phase"),
                ("unexpected", "extra"),
                ("stdout", {}),
                ("stderr", []),
            )
        ]
        scenarios.extend(
            ("packaged-root-generated-test", "cases", mutation)
            for mutation in (
                "missing",
                "duplicate",
                "foreign",
                "failed",
                "extra-field",
                "duplicate-key",
            )
        )
        scenarios.append(("packaged-project-execution", "stdout", "  "))
        for phase, field, value in scenarios:

            def altered(recorder, document, phase=phase, field=field, value=value):
                if isinstance(document, dict) and document.get("phase") == phase:
                    document = dict(document)
                    if field in {"stdout", "stderr"}:
                        document[field + "_identity"] = original(recorder, value).uri
                    elif field == "cases":
                        raw = dict(recorder.entries)[
                            ContentIdentity.parse_uri(document["stdout_identity"])
                        ]
                        observed = json.loads(json.loads(raw))
                        if value == "missing":
                            observed["cases"].pop()
                        elif value == "duplicate":
                            observed["cases"].append(observed["cases"][0])
                        elif value == "foreign":
                            observed["cases"][0]["case_id"] = "foreign-case"
                        elif value == "failed":
                            observed["cases"][0]["outcome"] = "failed"
                        elif value == "extra-field":
                            observed["cases"][0]["extra"] = True
                        output = json.dumps(observed)
                        if value == "duplicate-key":
                            output = output.replace(
                                '"cases":', '"cases":[],"cases":', 1
                            )
                        document["stdout_identity"] = original(recorder, output).uri
                    else:
                        document[field] = value
                return original(recorder, document)

            with patch.object(QualificationEvidenceRecorder, "remember_json", altered):
                result, captures, entries = self.typed_product_fixture()
            with self.subTest(phase=phase, field=field, value=value):
                self.assert_product_records_refused(
                    result, captures, entries, "root-(process|test|output)"
                )

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

    def test_product_reopening_requires_current_standard_authority(self):
        result, captures, entries = self.typed_product_fixture()
        driver = self.current_driver
        for current, reason in (
            (None, "standard-authority-invalid"),
            (
                replace(
                    driver,
                    framework_distribution_identity=canonical_identity(
                        "changed-distribution"
                    ),
                ),
                "standard-authority-mismatch",
            ),
            (
                replace(driver, policy_identity=canonical_identity("changed-policy")),
                "standard-authority-mismatch",
            ),
        ):
            with self.subTest(driver=current):
                self.current_driver = current
                self.assert_product_records_refused(result, captures, entries, reason)
        self.current_driver = driver

    def test_parity_requires_the_current_profile_and_clean_run_minimum(self):
        result, captures, entries = self.typed_product_fixture()
        profile = self.current_profile
        changes = (
            (None, "parity-authority-invalid"),
            (replace(profile, minimum_clean_runs=3), "parity-authority-invalid"),
            (
                replace(profile, source_command=("foreign-baseline",)),
                "parity-authority-mismatch",
            ),
            (
                replace(profile, generated_command=("foreign-generated",)),
                "parity-authority-mismatch",
            ),
            (
                replace(profile, covered_surface_ids=("foreign-surface",)),
                "parity-authority-mismatch",
            ),
            (
                replace(
                    profile,
                    cases=(
                        replace(profile.cases[0], expected_result={"changed": True}),
                        *profile.cases[1:],
                    ),
                ),
                "parity-authority-mismatch",
            ),
        )
        for current, reason in changes:
            with self.subTest(profile=current):
                self.current_profile = current
                self.assert_product_records_refused(result, captures, entries, reason)
        self.current_profile = profile

    def test_retained_parity_preserves_finite_numeric_observations(self):
        result, captures, entries = self.typed_product_fixture()
        original = QualificationEvidenceReader(
            entries, max_bytes=5_000_000, max_records=1000
        )
        parity = result.parity_evidence[1]
        case = parity.cases[0]
        for baseline, generated in (
            (b'{"value":1.25}', b'{"value":1.250}'),
            (b"18446744073709551616", b"18446744073709551616"),
        ):
            recorder = QualificationEvidenceRecorder(
                max_bytes=5_000_000, max_records=1000
            )
            for _, payload in entries:
                recorder.remember_bytes(payload)
            observation_ids = []
            for identity, output in (
                (case.baseline_observation_identity, baseline),
                (case.generated_observation_identity, generated),
            ):
                document = original.read_json(identity)
                document["stdout"] = recorder.remember_bytes(output).uri
                observation_ids.append(recorder.remember_json(document))
            changed_case = replace(
                case,
                baseline_observation_identity=observation_ids[0],
                generated_observation_identity=observation_ids[1],
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
            with self.subTest(baseline=baseline, generated=generated):
                actual = reopen_qualification_products(
                    QualificationEvidenceReader(
                        recorder.entries, max_bytes=5_000_000, max_records=1000
                    ),
                    qualification_identity=changed_result.identity,
                    run_identity=captures[0].run.run_identity,
                    exports_identity=captures[0].exports.identity,
                    max_package_bytes=4096,
                    component_lock=self.current_lock,
                    oracle=self.current_oracle,
                    current_recipes=self.current_recipes,
                    current_commands=self.current_commands,
                    current_profile=self.current_profile,
                    current_driver=self.current_driver,
                )
                self.assertEqual(actual, captures[0])

    def test_rehashed_parity_observations_cannot_preserve_a_false_pass(self):
        result, captures, entries = self.typed_product_fixture()
        original = QualificationEvidenceReader(
            entries, max_bytes=5_000_000, max_records=1000
        )
        parity = result.parity_evidence[1]
        case = parity.cases[0]
        for field, value, reason in (
            ("command", ["foreign-probe"], "parity-record-mismatch"),
            ("exit_status", 1, "parity-record-mismatch"),
            ("exit_status", False, "parity-record-mismatch"),
            ("json_output_valid", False, "parity-record-mismatch"),
            ("json_output_valid", 1, "parity-record-mismatch"),
            ("unexpected", "extra", "parity-record-mismatch"),
            ("stdout_bytes", b'{"value":false}', "parity-result-mismatch"),
            ("stdout_bytes", b'{"value":0,"value":0}', "parity-record-invalid"),
            ("stdout_bytes", b"NaN", "parity-record-invalid"),
            ("stdout_bytes", b"\xff", "parity-record-invalid"),
            ("stdout_bytes", b"x" * 513, "parity-output-limit"),
            ("stderr_bytes", b"x" * 513, "parity-output-limit"),
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

    def test_current_command_authority_is_complete_and_exact(self):
        result, captures, entries = self.typed_product_fixture()
        current = dict(self.current_commands)
        revision = next(iter(current))
        contract = current[revision]
        foreign = canonical_identity("foreign-command-authority")
        changes = [
            ({}, "command-authority-invalid"),
            ({**current, foreign: contract}, "command-authority-invalid"),
            ({**current, revision: object()}, "command-authority-invalid"),
        ]
        for field in (
            "locked_build_authority_identity",
            "build_system_resolver_identity",
            "build_system_toolchain_identity",
            "language_compiler_identity",
            "language_runtime_identity",
        ):
            changes.append(
                (
                    {**current, revision: replace(contract, **{field: foreign})},
                    "command-authority-mismatch",
                )
            )
        changes.append(
            (
                {
                    **current,
                    revision: replace(
                        contract,
                        library_import_surface=None,
                        artifact_export=replace(
                            contract.artifact_export, role="foreign-role"
                        ),
                    ),
                },
                "command-authority-mismatch",
            )
        )
        changed_command = replace(
            contract.commands[0],
            argv=(*contract.commands[0].argv, "changed-current-command"),
        )
        changes.append(
            (
                {
                    **current,
                    revision: replace(
                        contract, commands=(changed_command, *contract.commands[1:])
                    ),
                },
                "command-authority-mismatch",
            )
        )
        for commands, reason in changes:
            with self.subTest(authority=commands):
                self.current_commands = commands
                self.assert_product_records_refused(result, captures, entries, reason)
        self.current_commands = current

    def test_rehashed_index_substitutions_block_package_reads(self):
        original = QualificationEvidenceRecorder.remember_json
        for field, value in (
            ("component", canonical_identity("foreign-component").uri),
            ("tree", canonical_identity("foreign-tree").uri),
            ("indexer", "foreign-indexer@1"),
            ("path_count", -1),
            ("path_count", True),
            ("path_count", "3"),
            ("unexpected", "extra"),
        ):

            def altered(recorder, document, field=field, value=value):
                if (
                    isinstance(document, dict)
                    and document.get("indexer") == "local-tree@1"
                ):
                    document = {**document, field: value}
                return original(recorder, document)

            with patch.object(QualificationEvidenceRecorder, "remember_json", altered):
                result, captures, entries = self.typed_product_fixture()
            with self.subTest(field=field, value=value):
                self.assert_product_records_refused(
                    result, captures, entries, "index-mismatch"
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

    def test_rehashed_source_custody_cannot_substitute_accepted_generation(self):
        original = QualificationEvidenceRecorder.remember_json
        for field in (
            "candidate_identity",
            "source_generation_identity",
            "source_tree_identity",
            "source_bom_identity",
            "managed_graph_identity",
            "generated_test_suite_identity",
            "unexpected",
        ):

            def altered(recorder, value, field=field):
                if (
                    isinstance(value, dict)
                    and value.get("schema")
                    == "literate-ai/local-generated-source-custody@1"
                ):
                    value = {**value, field: canonical_identity("foreign-custody").uri}
                return original(recorder, value)

            with (
                self.subTest(field=field),
                patch.object(QualificationEvidenceRecorder, "remember_json", altered),
            ):
                result, captures, entries = self.typed_product_fixture()
            reader = QualificationEvidenceReader(
                entries, max_bytes=5_000_000, max_records=1000
            )
            with (
                self.subTest(field=field),
                patch.object(reader, "read_bytes", wraps=reader.read_bytes) as reads,
                self.assertRaisesRegex(
                    QualificationCaptureError, "source-custody-mismatch"
                ),
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

    def test_missing_second_run_process_blocks_product_bytes(self):
        result, captures, entries = self.typed_product_fixture()
        complete = QualificationEvidenceReader(
            entries, max_bytes=5_000_000, max_records=1000
        )
        lifecycle = reopen_qualification_lifecycle(
            complete, captures[1].run.lifecycle_result_identity
        )
        node = lifecycle.node_results[0]
        build_observation = complete.read_json(
            node.build_evidence.build_observation_identity
        )
        custody = complete.read_json(node.build_evidence.artifact_custody_identity)
        final_tree = complete.read_json(
            ContentIdentity.parse_uri(custody["artifact_tree_identity"])
        )
        manifest = next(
            item
            for item in final_tree["files"]
            if item["path"] == "artifact-manifest.json"
        )
        source_manifest = complete.read_json(
            node.source_output.candidate.source_manifest_identity
        )
        invocation_id = ContentIdentity.from_dict(
            source_manifest["invocation_identity"]
        )
        invocation = complete.read_json(invocation_id)
        source_tree = complete.read_json(
            node.source_output.candidate.source_bundle_identity
        )
        grant = complete.read_json(node.authorization_identity)
        request_id = ContentIdentity.parse_uri(grant["request_digest"])
        authorization_record = next(
            (identity, complete.read_json(identity))
            for identity, _ in entries
            if b'"build_intent_identity"' in complete.read_bytes(identity)
            and b'"grant"' in complete.read_bytes(identity)
            and complete.read_json(identity).get("grant") == grant
        )
        command_contract = self.current_commands[node.component_revision]
        self.assertTrue(command_contract.is_library)
        command_records = [command_contract, *command_contract.commands]
        root = lifecycle.root_integration
        root_process_ids = (
            root.root_generated_integration_test_identity,
            root.packaged_execution_identity,
        )
        root_output_ids = tuple(
            ContentIdentity.parse_uri(complete.read_json(identity)[field])
            for identity in root_process_ids
            for field in ("stdout_identity", "stderr_identity")
        )
        parity = result.parity_evidence[1]
        parity_case = parity.cases[0]
        parity_ids = (
            result.runs[0].source_snapshot_identity,
            ContentIdentity.parse_uri(self.current_profile.identity),
            result.case_map.identity,
            result.case_map.verifier_identity,
            parity.identity,
            parity_case.identity,
            parity_case.case_identity,
            parity_case.baseline_observation_identity,
            parity_case.generated_observation_identity,
            *(
                ContentIdentity.parse_uri(complete.read_json(identity)[field])
                for identity in (
                    parity_case.baseline_observation_identity,
                    parity_case.generated_observation_identity,
                )
                for field in ("stdout", "stderr")
            ),
        )
        for missing in (
            *parity_ids,
            *root_process_ids,
            *root_output_ids,
            *(record.identity for record in command_records),
            node.index_identity,
            node.authorization_identity,
            request_id,
            authorization_record[0],
            ContentIdentity.from_dict(authorization_record[1]["build_intent_identity"]),
            node.execution_evidence.observation_identity,
            node.source_output.candidate.identity,
            node.source_output.candidate.source_manifest_identity,
            node.source_output.candidate.source_bundle_identity,
            invocation_id,
            ContentIdentity.from_dict(invocation["execution_plan_identity"]),
            canonical_identity(invocation["stage_request"]),
            node.source_output.provenance.route_decision_identities[0],
            ContentIdentity.parse_uri(
                BlobRef.from_dict(source_manifest["stage_output_record"]).identity
            ),
            *(
                ContentIdentity.parse_uri(BlobRef.from_dict(item["blob"]).identity)
                for item in source_tree["files"]
            ),
            node.build_evidence.source_custody_identity,
            node.build_evidence.build_observation_identity,
            node.build_evidence.artifact_custody_identity,
            ContentIdentity.parse_uri(build_observation["artifact_tree_identity"]),
            ContentIdentity.parse_uri(custody["artifact_tree_identity"]),
            ContentIdentity.parse_uri("sha256:" + manifest["sha256"]),
            node.generated_test_evidence.cases[0].observation_identity,
            node.generated_test_evidence.generated_test_suite_identity,
            node.build_evidence.source_sbom.bom_identity,
            node.build_evidence.resolved_sbom.bom_identity,
        ):
            reader = QualificationEvidenceReader(
                tuple(item for item in entries if item[0] != missing),
                max_bytes=5_000_000,
                max_records=1000,
            )
            with (
                patch.object(reader, "read_bytes", wraps=reader.read_bytes) as reads,
                self.assertRaisesRegex(QualificationCaptureError, "record-missing"),
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

    def test_other_clean_run_requires_exact_independent_oracle_observations(self):
        result, captures, entries = self.typed_product_fixture(
            corrupt_second_oracle=True
        )
        reader = QualificationEvidenceReader(
            entries, max_bytes=5_000_000, max_records=1000
        )
        with (
            patch.object(reader, "read_bytes", wraps=reader.read_bytes) as reads,
            self.assertRaisesRegex(
                QualificationCaptureError, "oracle-observation-mismatch"
            ),
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

    def test_current_suite_cannot_mask_foreign_source_recipe(self):
        result, captures, entries = self.typed_product_fixture(
            foreign_candidate_recipe=True
        )
        reader = QualificationEvidenceReader(
            entries, max_bytes=5_000_000, max_records=1000
        )
        with (
            patch.object(reader, "read_bytes", wraps=reader.read_bytes) as reads,
            self.assertRaisesRegex(QualificationCaptureError, "suite-source-mismatch"),
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

    def test_product_reopening_requires_every_current_component_recipe(self):
        result, captures, entries = self.typed_product_fixture()
        reader = QualificationEvidenceReader(
            entries, max_bytes=5_000_000, max_records=1000
        )
        revision = next(
            key
            for key in self.current_recipes
            if key != self.current_lock.root_revision
        )
        recipe = self.current_recipes[revision]
        alternatives = (
            {
                key: value
                for key, value in self.current_recipes.items()
                if key != revision
            },
            {**self.current_recipes, canonical_identity("extra-component"): recipe},
            {
                **self.current_recipes,
                revision: replace(recipe, recipe_id=recipe.recipe_id + "-changed"),
            },
            {**self.current_recipes, revision: object()},
        )
        package_ids = {
            ContentIdentity.parse_uri(ref.identity)
            for capture in captures
            for ref, _ in capture.blobs
        }
        for recipes in alternatives:
            with (
                self.subTest(recipes=tuple(recipes)),
                patch.object(reader, "read_bytes", wraps=reader.read_bytes) as reads,
                self.assertRaises(QualificationCaptureError),
            ):
                reopen_qualification_products(
                    reader,
                    qualification_identity=result.identity,
                    run_identity=captures[0].run.run_identity,
                    exports_identity=captures[0].exports.identity,
                    component_lock=self.current_lock,
                    oracle=self.current_oracle,
                    current_recipes=recipes,
                    current_commands=self.current_commands,
                    current_profile=self.current_profile,
                    current_driver=self.current_driver,
                    max_package_bytes=4096,
                )
            self.assertTrue(
                package_ids.isdisjoint(call.args[0] for call in reads.call_args_list)
            )

    def test_product_reopening_requires_current_oracle_authority(self):
        result, captures, entries = self.typed_product_fixture()
        reader = QualificationEvidenceReader(
            entries, max_bytes=5_000_000, max_records=1000
        )
        foreign = replace(
            self.current_oracle,
            public_interface_identities=(canonical_identity("foreign"),),
        )
        with self.assertRaisesRegex(
            QualificationCaptureError, "oracle-authority-mismatch"
        ):
            reopen_qualification_products(
                reader,
                qualification_identity=result.identity,
                run_identity=captures[0].run.run_identity,
                exports_identity=captures[0].exports.identity,
                component_lock=self.current_lock,
                oracle=foreign,
                current_recipes=self.current_recipes,
                current_commands=self.current_commands,
                current_profile=self.current_profile,
                current_driver=self.current_driver,
                max_package_bytes=4096,
            )

    def test_other_clean_run_cannot_claim_foreign_workspace_membership(self):
        result, captures, entries = self.typed_product_fixture()
        substituted = replace(
            result,
            runs=(
                result.runs[0],
                replace(
                    result.runs[1],
                    node_workspace_identities=(
                        canonical_identity("foreign-workspace"),
                    ),
                ),
            ),
        )
        recorder = QualificationEvidenceRecorder(max_bytes=5_000_000, max_records=1000)
        for _, payload in entries:
            recorder.remember_bytes(payload)
        recorder.remember_json(substituted.to_dict())
        reader = QualificationEvidenceReader(
            recorder.entries, max_bytes=5_000_000, max_records=1000
        )
        with (
            patch.object(reader, "read_bytes", wraps=reader.read_bytes) as reads,
            self.assertRaisesRegex(QualificationCaptureError, "run-mismatch"),
        ):
            reopen_qualification_products(
                reader,
                qualification_identity=substituted.identity,
                run_identity=captures[0].run.run_identity,
                exports_identity=captures[0].exports.identity,
                max_package_bytes=4096,
                component_lock=self.current_lock,
                oracle=self.current_oracle,
                current_recipes=self.current_recipes,
                current_commands=self.current_commands,
                current_profile=self.current_profile,
                current_driver=self.current_driver,
            )
        package_ids = {
            ContentIdentity.parse_uri(ref.identity)
            for capture in captures
            for ref, _ in capture.blobs
        }
        self.assertTrue(
            package_ids.isdisjoint(call.args[0] for call in reads.call_args_list)
        )

    def test_product_reopening_requires_complete_lifecycle_before_package_reads(self):
        self.library_authority_fixture()
        adapter, plan, result, _, _ = self.adapter_fixture()
        adapter.qualify(plan)
        captured = adapter.library_captures[0]
        # This older producer contract fixture has coherent root and receipt
        # metadata, but omits the complete typed lifecycle. Reject the missing
        # lifecycle fields before reading its retained package bytes.
        reader = QualificationEvidenceReader(
            adapter.evidence_blobs, max_bytes=1_000_000, max_records=1000
        )
        with (
            patch.object(reader, "read_bytes", wraps=reader.read_bytes) as reads,
            self.assertRaisesRegex(QualificationCaptureError, "record-missing"),
        ):
            reopen_qualification_products(
                reader,
                qualification_identity=result.identity,
                run_identity=captured.run.run_identity,
                exports_identity=captured.exports.identity,
                max_package_bytes=4096,
                component_lock=self.current_lock,
                oracle=self.current_oracle,
                current_recipes=self.current_recipes,
                current_commands=self.current_commands,
                current_profile=self.current_profile,
                current_driver=self.current_driver,
            )
        package_ids = {
            ContentIdentity.parse_uri(ref.identity) for ref, _ in captured.blobs
        }
        self.assertTrue(
            package_ids.isdisjoint(call.args[0] for call in reads.call_args_list)
        )

    def test_reopen_refuses_a_valid_export_graph_from_another_root_before_blob_reads(
        self,
    ):
        self.library_authority_fixture()
        adapter, plan, result, _, _ = self.adapter_fixture()
        adapter.qualify(plan)
        captured = adapter.library_captures[0]
        driver = canonical_identity("substituted-build-driver")
        graph = create_artifact_build_graph(
            build_system_driver_identity=driver,
            manifests=tuple(
                replace(manifest, build_system_driver_identity=driver)
                for manifest in captured.exports.graph.manifests
            ),
            link_roots=captured.exports.link_plan.resolved_root_artifact_identities,
        )
        substituted = RetainedLibraryExportSet(
            graph, graph.link_plans[0].identity, captured.exports.libraries
        )
        recorder = QualificationEvidenceRecorder(max_bytes=1_000_000, max_records=1000)
        for _, payload in adapter.evidence_blobs:
            recorder.remember_bytes(payload)
        recorder.remember_json(substituted.to_dict())
        # The foreign graph is internally valid and contains the same package
        # exports, but does not belong to the run's root integration. Package
        # bytes are present; the foreign graph must still refuse before they
        # can be returned as qualified products.
        reader = QualificationEvidenceReader(
            recorder.entries, max_bytes=1_000_000, max_records=1000
        )
        with self.assertRaisesRegex(QualificationCaptureError, "root-export-mismatch"):
            reopen_qualification_products(
                reader,
                qualification_identity=result.identity,
                run_identity=captured.run.run_identity,
                exports_identity=substituted.identity,
                max_package_bytes=4096,
                component_lock=self.current_lock,
                oracle=self.current_oracle,
                current_recipes=self.current_recipes,
                current_commands=self.current_commands,
                current_profile=self.current_profile,
                current_driver=self.current_driver,
            )

    def test_authority_drift_after_runs_exposes_no_capture_or_package_reads(self):
        adapter, plan, _, readers, _ = self.adapter_fixture()
        adapter.lifecycle.require_current_authority.side_effect = RuntimeError("drift")
        with self.assertRaisesRegex(RuntimeError, "drift"):
            adapter.qualify(plan)
        for reader in readers:
            reader.assert_not_called()
        self.assertEqual(adapter.library_captures, ())
        self.assertEqual(adapter.evidence_blobs, ())
        self.assertEqual(adapter.lifecycle.product_sources, {})
        self.assertIsNone(adapter.lifecycle.evidence_recorder)

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

    def test_later_capture_failure_exposes_no_partial_result(self):
        adapter, plan, _, _, _ = self.adapter_fixture(corrupt_second=True)
        with self.assertRaisesRegex(QualificationCaptureError, "blob-mismatch"):
            adapter.qualify(plan)
        self.assertEqual(adapter.library_captures, ())
        self.assertEqual(adapter.lifecycle.product_sources, {})

    def test_budget_covers_all_runs_before_each_blob_read(self):
        baseline, baseline_plan, _, _, content = self.adapter_fixture()
        baseline.qualify(baseline_plan)
        package_ids = {
            ContentIdentity.parse_uri(reference.identity)
            for capture in baseline.library_captures
            for reference, _ in capture.blobs
        }
        metadata_bytes = sum(
            len(payload)
            for identity, payload in baseline.evidence_blobs
            if identity not in package_ids
        )
        adapter, plan, _, readers, _ = self.adapter_fixture(
            byte_limit=metadata_bytes + 2 * len(content) - 1
        )
        with self.assertRaisesRegex(QualificationCaptureError, "byte-limit"):
            adapter.qualify(plan)
        readers[0].assert_called_once()
        readers[1].assert_not_called()
        self.assertEqual(adapter.library_captures, ())

    def test_qualification_metadata_budget_refuses_before_package_reads(self):
        adapter, plan, _, readers, _ = self.adapter_fixture(byte_limit=1)
        with self.assertRaisesRegex(QualificationCaptureError, "byte-limit"):
            adapter.qualify(plan)
        for reader in readers:
            reader.assert_not_called()
        self.assertEqual(adapter.library_captures, ())
        self.assertEqual(adapter.evidence_blobs, ())
        self.assertEqual(adapter.lifecycle.product_sources, {})

    def test_retained_bytes_survive_original_custody_removal(self):
        run, exports, acceptance, content = fixture()
        reference = acceptance.build.exports[0].blob
        custody = {reference: content}
        captured = capture_qualification_run(
            run, exports, (acceptance,), read_blob=custody.__getitem__, max_bytes=1024
        )
        custody.clear()
        self.assertEqual(captured.blobs, ((reference, content),))
        self.assertEqual(captured.acceptances, (acceptance,))
        self.assertEqual(captured.run, run)

    def test_other_run_evidence_refuses_before_reading_packages(self):
        run, exports, acceptance, _ = fixture()
        for changes in (
            {"acceptance_evidence_identities": (canonical_identity("other"),)},
            {"build_evidence_identities": (canonical_identity("other"),)},
            {"generated_test_total": run.generated_test_total + 1},
        ):
            with self.subTest(changes=changes):
                reader = Mock()
                with self.assertRaisesRegex(QualificationCaptureError, "run-mismatch"):
                    capture_qualification_run(
                        replace(run, **changes),
                        exports,
                        (acceptance,),
                        read_blob=reader,
                        max_bytes=1024,
                    )
                reader.assert_not_called()

    def test_wrong_target_refuses_before_reading_packages(self):
        run, exports, acceptance, _ = fixture()
        reader = Mock()
        with self.assertRaisesRegex(QualificationCaptureError, "export-mismatch"):
            capture_qualification_run(
                replace(run, target_profile_identity=canonical_identity("other")),
                exports,
                (acceptance,),
                read_blob=reader,
                max_bytes=1024,
            )
        reader.assert_not_called()

    def test_budget_is_checked_before_reading_packages(self):
        run, exports, acceptance, content = fixture()
        for limit in (0, -1, True, len(content) - 1):
            with self.subTest(limit=limit):
                reader = Mock()
                with self.assertRaises(QualificationCaptureError):
                    capture_qualification_run(
                        run, exports, (acceptance,), read_blob=reader, max_bytes=limit
                    )
                reader.assert_not_called()

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
