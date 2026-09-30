"""Production source-only adapter tests with a deterministic cached delegate seam."""

from __future__ import annotations

import hashlib
import inspect
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import literate_ai.adapters.models.coding_cli as coding_cli_adapter
from literate_ai.adapters.builders import canonical_tree_digest
from literate_ai.adapters.cache import CachedCodingCliSourceGenerator
from literate_ai.adapters.dependencies import build_cyclonedx_bom
from literate_ai.adapters.lifecycle import local_generated_source_tree_identity
from literate_ai.adapters.models import CodingCliGeneration
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
    capture_qualification_source_records,
    verify_qualification_generation_records,
    verify_qualification_source_records,
)
from literate_ai.adapters.source_generation import (
    CachedCodingCliSourceGenerationError,
    CachedCodingCliSourceGenerationRunner,
    CodingCliSourceGenerationInvocation,
)
from literate_ai.adapters.standard_project import (
    FilesystemStandardSourceGenerationAdapter,
)
from literate_ai.application import (
    GenerationExecutionPlan,
    StandardSourceCacheMembership,
    prepare_component_generation_nodes,
)
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    AuthoredBinaryAsset,
    BlobRef,
    ContentIdentity,
    CycloneDxLifecycle,
    SourceCacheModelBinding,
    SourceDerivationCacheKey,
    SourceGenerationDisposition,
    SourceGenerationResumeCandidate,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.generated_tests import (
    GENERATED_TEST_SUITE_PATH,
    GENERATED_TEST_SUITE_SCHEMA,
    GeneratedTestSuiteError,
)
from literate_ai.storage import FileSystemCAS
from tests.unit.test_application_generation import router, stages
from tests.unit.test_component_node_generation_preparation import (
    FilesystemComponentWorkspaceAllocator,
    LockedComponentNodePreparationAdapter,
    _budget,
    _fixture,
)


def _test_manifest(recipe) -> str:
    reference = recipe.non_acceptance_document_paths[0]
    return json.dumps(
        {
            "schema": GENERATED_TEST_SUITE_SCHEMA,
            "recipe_identity": recipe.identity,
            "generation_mode": "major-rebuild",
            "cases": [
                {
                    "case_id": "component-example",
                    "category": "example",
                    "specification_refs": [reference],
                    "arguments": [{"component": "example"}],
                    "expected_result": {"component": "example"},
                },
                {
                    "case_id": "component-boundary",
                    "category": "boundary",
                    "specification_refs": [reference],
                    "arguments": [{"component": "boundary"}],
                    "expected_result": {"component": "boundary"},
                },
                {
                    "case_id": "component-invariant",
                    "category": "invariant",
                    "specification_refs": [reference],
                    "arguments": [{"component": "invariant"}],
                    "expected_result": {"component": "invariant"},
                },
            ],
        },
        sort_keys=True,
    )


class _FakeCachedGenerator(CachedCodingCliSourceGenerator):
    """Cached-generator seam: one delegate result, then deterministic replay."""

    def __init__(self) -> None:
        self.delegate = SimpleNamespace(
            source_intelligence_provider=None,
            source_intelligence_mode="off",
        )
        self.calls = []
        self.delegate_calls = 0
        self.accept_invocations = 0
        self.publications = 0
        self.provider_evidence_identity: str | None = None
        self._cached: CodingCliGeneration | None = None
        self._pending: CodingCliGeneration | None = None

    def planned_request_identity(
        self, recipe, *, execution_plan, stage_request, bounded_prompt=None
    ) -> ContentIdentity:
        return canonical_identity(
            {
                "recipe": recipe.identity,
                "execution_plan": execution_plan.identity.uri,
                "stage_request": stage_request,
                "bounded_prompt_identity": (
                    None
                    if bounded_prompt is None
                    else "sha256:" + hashlib.sha256(bounded_prompt).hexdigest()
                ),
            }
        )

    def derivation_cache_key(
        self, recipe, *, execution_plan, stage_request, bounded_prompt=None
    ) -> SourceDerivationCacheKey:
        return SourceDerivationCacheKey(
            recipe_identity=ContentIdentity.parse_uri(recipe.identity),
            execution_plan_identity=execution_plan.identity,
            coding_cli_tool_binding_identity=ContentIdentity.parse_uri(
                "sha256:" + "b" * 64
            ),
            model_binding=SourceCacheModelBinding("codex", "fixture-model"),
            request_identity=self.planned_request_identity(
                recipe,
                execution_plan=execution_plan,
                stage_request=stage_request,
                bounded_prompt=bounded_prompt,
            ),
        )

    def generate(
        self,
        recipe,
        *,
        output_root,
        execution_plan,
        stage_request,
        bounded_prompt=None,
    ) -> CodingCliGeneration:
        self.calls.append(
            (recipe, Path(output_root), execution_plan, stage_request, bounded_prompt)
        )
        root = Path(output_root)
        root.mkdir(parents=True, exist_ok=True)
        if any(root.iterdir()):
            raise AssertionError("fake received a non-empty generation workspace")
        if self._cached is None:
            self.delegate_calls += 1
            assert recipe.managed_sbom_graph is not None
            authority_components, authority_edges = (
                coding_cli_adapter._recipe_authority_sbom(recipe)
            )
            sbom_content, sbom = build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=recipe.managed_sbom_graph,
                additional_components=authority_components,
                additional_edges=authority_edges,
            )
            test_manifest = _test_manifest(recipe)
            files = {
                "source/main.py": "print('source-only')\n",
                GENERATED_TEST_SUITE_PATH: test_manifest,
                CYCLONEDX_SOURCE_SBOM_PATH: sbom_content.decode("utf-8"),
            }
            final_stage = execution_plan.model_stages[-1]
            final_route = execution_plan.route_decisions[-1]
            self._cached = CodingCliGeneration(
                files=files,
                coding_cli="codex",
                executable="/tools/codex",
                model="fixture-model",
                recipe_identity=recipe.identity,
                command=("/tools/codex", "generate"),
                request_identity=self.planned_request_identity(
                    recipe,
                    execution_plan=execution_plan,
                    stage_request=stage_request,
                    bounded_prompt=bounded_prompt,
                ).uri,
                execution_plan_identity=execution_plan.identity.uri,
                requested_model_stages=(final_stage.stage_id,),
                requested_route_decision_digests=(final_route.digest,),
                executable_identity="sha256:" + "e" * 64,
                command_identity="sha256:" + "d" * 64,
                coding_cli_selection_identity="sha256:" + "c" * 64,
                coding_cli_tool_binding_identity="sha256:" + "b" * 64,
                isolation_profile="fixture",
                hermetic=False,
                environment_keys=(),
                generated_test_suite_identity=(
                    "sha256:"
                    + hashlib.sha256(test_manifest.encode("utf-8")).hexdigest()
                ),
                source_sbom=sbom,
                source_intelligence_status="off",
                provider_evidence_identity=self.provider_evidence_identity,
            )
            self._pending = self._cached
        for path, content in self._cached.files.items():
            target = root.joinpath(*path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        return self._cached

    def accept(self, generation) -> None:
        self.accept_invocations += 1
        if generation is self._pending:
            self.publications += 1
            self._pending = None


class CachedCodingCliSourceGenerationRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name).resolve(strict=True)
        self.root = root
        snapshot, execution = _fixture()
        self.snapshot = snapshot
        adapter = LockedComponentNodePreparationAdapter()
        workspace_root = root / "workspaces"
        workspace_root.mkdir()
        allocator = FilesystemComponentWorkspaceAllocator(workspace_root)
        self.node = prepare_component_generation_nodes(
            execution,
            authority=snapshot,
            authority_lock_identity=adapter.authority_lock_identity,
            authority_guard=adapter.guard,
            node_projector=adapter.project,
            workspace_allocator=allocator.allocate,
            framework_envelope=lambda projection: projection.recipe.prompt().encode(
                "utf-8"
            ),
            budget=_budget(),
        )[0]
        stage_values = stages()
        route_selector = router()
        self.execution_plan = GenerationExecutionPlan(
            self.node.definition.workflow_definition,
            self.node.definition.routing_policy,
            stage_values,
            tuple(route_selector.select(stage.policy) for stage in stage_values),
        )
        self.stage_request = {
            "stage_id": self.execution_plan.model_stages[-1].stage_id,
            "prior_stage_outputs": {"plan": {"approved": True}},
            "input_identity": canonical_identity({"stage": "generate"}).to_dict(),
        }
        self.application_root = snapshot.authority.lock.root_revision
        self.readiness = canonical_identity({"readiness": "fixture"})
        self.invocation = CodingCliSourceGenerationInvocation.create(
            self.execution_plan,
            self.stage_request,
            application_root_revision_identity=self.application_root,
            readiness_identity=self.readiness,
        )
        self.generator = _FakeCachedGenerator()
        self.cas = FileSystemCAS(root / "cas")
        self.runner = CachedCodingCliSourceGenerationRunner(
            self.generator,
            cas=self.cas,
            invocation_provider=lambda _node: self.invocation,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _retained_input(self):
        from literate_ai.adapters.retained_source import RetainedSourceInput

        original = self.generator.generate(
            self.node.recipe,
            output_root=self.root / "retained",
            execution_plan=self.execution_plan,
            stage_request=self.stage_request,
        )
        retained = RetainedSourceInput.capture(
            self.root / "retained/source",
            component_lock_identity=self.node.recipe.component_lock_identity,
            project_authority_identity=canonical_identity({"project": "fixture"}),
            target="host",
        )
        self.runner.retained_source = retained
        self.generator.generate = mock.Mock(
            side_effect=AssertionError("must not generate")
        )
        return retained, original

    def test_retained_input_preserves_bytes_and_has_no_model_provenance(self):
        from literate_ai.contracts import SourceGenerationProvenance

        generated_key = self.runner.planned_cache_key(self.node)
        retained, original = self._retained_input()
        retained.require_authorization(retained.identity.uri)
        retained_key = self.runner.planned_cache_key(self.node)
        self.assertNotEqual(
            generated_key.accepted_source_lookup_identity,
            retained_key.accepted_source_lookup_identity,
        )
        output = self.runner(self.node)
        from literate_ai.adapters.cache.standard import (
            StandardSourceCachePublicationError,
            _require_retained_cache_namespace,
        )

        _require_retained_cache_namespace(output.provenance, retained_key)
        with self.assertRaises(StandardSourceCachePublicationError):
            _require_retained_cache_namespace(output.provenance, generated_key)
        self.assertEqual(output.candidate.tree_identity, retained.tree_identity)
        self.assertEqual(output.provenance.retained_source_identity, retained.identity)
        self.assertEqual(output.provenance.model_stage_output_identities, ())
        self.assertEqual(output.provenance.route_decision_identities, ())
        self.assertEqual(
            SourceGenerationProvenance.from_dict(output.provenance.to_dict()),
            output.provenance,
        )
        for name, text in original.files.items():
            self.assertEqual(
                (Path(self.node.workspace.locator) / name).read_bytes(), text.encode()
            )
        self.generator.generate.assert_not_called()

    def test_retained_source_drift_prevents_candidate_and_publication(self):
        from literate_ai.adapters.retained_source import RetainedSourceError

        retained, _ = self._retained_input()
        output = self.runner(self.node)
        (retained.root / "main.py").write_text("changed\n")
        with self.assertRaises(RetainedSourceError):
            self.runner.cache_key_for_candidate(output.candidate)

    def test_retained_input_cannot_claim_model_provenance(self):
        self._retained_input()
        output = self.runner(self.node)
        with self.assertRaises(ValueError):
            replace(
                output.provenance,
                model_stage_output_identities=(canonical_identity({"fake": True}),),
            )

    def test_retained_input_wrong_lock_refused_before_generation(self):
        retained, _ = self._retained_input()
        self.runner.retained_source = replace(
            retained, component_lock_identity=canonical_identity({"wrong": True})
        )
        with self.assertRaisesRegex(
            CachedCodingCliSourceGenerationError, "Component lock"
        ):
            self.runner(self.node)

    def test_retained_input_invalid_test_manifest_is_not_repaired(self):
        from literate_ai.adapters.retained_source import RetainedSourceInput

        retained, _ = self._retained_input()
        (retained.root / GENERATED_TEST_SUITE_PATH.removeprefix("source/")).write_text(
            "{}"
        )
        self.runner.retained_source = RetainedSourceInput.capture(
            retained.root,
            component_lock_identity=retained.component_lock_identity,
            project_authority_identity=retained.project_authority_identity,
            target="host",
        )
        with self.assertRaises(GeneratedTestSuiteError):
            self.runner(self.node)

    def test_generated_cache_ownership_precedes_candidate_cas_creation(self) -> None:
        adapter = object.__new__(FilesystemStandardSourceGenerationAdapter)
        adapter.project_root = self.root
        adapter.cache_root = self.root / "generated"
        adapter.cas_root = adapter.cache_root / "candidate-cas"
        events: list[str] = []

        def mark_cache(*_args, **_kwargs):
            events.append("cache-marker")
            adapter.cache_root.mkdir()
            return adapter.cache_root

        def create_cas(_root):
            events.append("candidate-cas")
            return object()

        with (
            mock.patch(
                "literate_ai.adapters.standard_project.ensure_cache_directory",
                side_effect=mark_cache,
            ),
            mock.patch(
                "literate_ai.adapters.standard_project.FileSystemCAS",
                side_effect=create_cas,
            ),
            mock.patch(
                "literate_ai.adapters.standard_project.admit_locked_authored_assets",
                side_effect=RuntimeError("stop after cache setup"),
            ),
            self.assertRaisesRegex(RuntimeError, "stop after cache setup"),
        ):
            adapter.generate(object(), output_root=self.root / "output")

        self.assertEqual(events, ["cache-marker", "candidate-cas"])

    def _cas_manifest(self, identity: ContentIdentity) -> dict[str, object]:
        reference = next(
            item for item in self.cas.iter_refs() if item.identity == identity.uri
        )
        return self.cas.get_manifest(reference)

    def test_retained_source_bundle_survives_complete_scratch_cleanup(self) -> None:
        output = self.runner(self.node)
        candidate = output.candidate
        recorder = QualificationEvidenceRecorder(max_bytes=5_000_000, max_records=1000)
        capture_qualification_source_records(
            recorder, candidate=candidate, cas=self.cas
        )
        manifest = self._cas_manifest(candidate.source_manifest_identity)
        tree = self.cas.get_manifest(BlobRef.from_dict(manifest["tree_record"]))
        expected = {
            item["path"]: self.cas.get_bytes(BlobRef.from_dict(item["blob"]))
            for item in tree["files"]
        }
        self.temporary.cleanup()
        reader = QualificationEvidenceReader(
            recorder.entries, max_bytes=5_000_000, max_records=1000
        )
        verify_qualification_source_records(reader, candidate=candidate)
        verify_qualification_generation_records(reader, source_output=output)
        stage = reader.read_json(output.provenance.model_stage_output_identities[0])
        for changes in (
            {name: canonical_identity("foreign-stage").to_dict()}
            for name in (
                "planned_request_identity",
                "tree_identity",
                "execution_plan_identity",
                "stage_request_identity",
            )
        ):
            changed_stage = {**stage, **changes}
            changed_stage_id = recorder.remember_json(changed_stage)
            stage_ref = replace(
                BlobRef.from_dict(manifest["stage_output_record"]),
                digest=changed_stage_id.digest,
                size=len(canonical_json_bytes(changed_stage)),
            )
            changed_manifest_id = recorder.remember_json(
                {**manifest, "stage_output_record": stage_ref.to_dict()}
            )
            changed_candidate = replace(
                candidate, source_manifest_identity=changed_manifest_id
            )
            changed_provenance = replace(
                output.provenance,
                candidate_identity=changed_candidate.identity,
                model_stage_output_identities=(changed_stage_id,),
            )
            changed_output = replace(
                output,
                candidate=changed_candidate,
                candidate_identity=changed_candidate.identity,
                provenance=changed_provenance,
                provenance_identity=changed_provenance.identity,
            )
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(
                    QualificationCaptureError, "generation-mismatch"
                ),
            ):
                verify_qualification_generation_records(
                    QualificationEvidenceReader(
                        recorder.entries, max_bytes=5_000_000, max_records=1000
                    ),
                    source_output=changed_output,
                )
        self.assertEqual(
            reader.read_json(self.invocation.identity),
            self.invocation.identity_document(),
        )
        self.assertEqual(
            reader.read_bytes(self.execution_plan.identity),
            canonical_json_bytes(self.execution_plan.to_dict()),
        )
        self.assertEqual(
            reader.read_json(canonical_identity(self.invocation.stage_request)),
            self.invocation.stage_request,
        )
        route = self.execution_plan.route_decisions[-1]
        self.assertEqual(
            reader.read_json(ContentIdentity.parse_uri(route.digest)), route.to_dict()
        )
        self.assertEqual(reader.read_json(candidate.source_manifest_identity), manifest)
        self.assertEqual(reader.read_json(candidate.source_bundle_identity), tree)
        for item in tree["files"]:
            self.assertEqual(
                reader.read_bytes(
                    ContentIdentity.parse_uri(BlobRef.from_dict(item["blob"]).identity)
                ),
                expected[item["path"]],
            )
        reader.read_json(
            ContentIdentity.parse_uri(
                BlobRef.from_dict(manifest["stage_output_record"]).identity
            )
        )
        for change in ("bom-path", "blob-size"):
            files = json.loads(json.dumps(tree["files"]))
            if change == "bom-path":
                next(
                    item for item in files if item["path"] == CYCLONEDX_SOURCE_SBOM_PATH
                )["path"] = "source/foreign-bom.json"
            else:
                files[0]["blob"]["size"] += 1
            files.sort(key=lambda item: item["path"])
            changed_tree_identity = canonical_identity(
                [
                    {
                        "path": item["path"],
                        "size": item["blob"]["size"],
                        "digest": BlobRef.from_dict(item["blob"]).identity,
                    }
                    for item in files
                ]
            )
            document = {
                **tree,
                "files": files,
                "tree_identity": changed_tree_identity.to_dict(),
            }
            changed_tree = recorder.remember_json(document)
            tree_ref = replace(
                BlobRef.from_dict(manifest["tree_record"]),
                digest=changed_tree.digest,
                size=len(canonical_json_bytes(document)),
            )
            changed_manifest = recorder.remember_json(
                {
                    **manifest,
                    "tree_identity": changed_tree_identity.to_dict(),
                    "tree_record": tree_ref.to_dict(),
                }
            )
            changed_candidate = replace(
                candidate,
                tree_identity=changed_tree_identity,
                source_bundle_identity=changed_tree,
                source_manifest_identity=changed_manifest,
            )
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(
                    QualificationCaptureError, "source-records-mismatch"
                ),
            ):
                verify_qualification_source_records(
                    QualificationEvidenceReader(
                        recorder.entries, max_bytes=5_000_000, max_records=1000
                    ),
                    candidate=changed_candidate,
                )

    def test_retained_source_capture_refuses_missing_file_and_oversized_manifest(
        self,
    ) -> None:
        candidate = self.runner(self.node).candidate
        with mock.patch.object(
            self.cas, "get_bytes", wraps=self.cas.get_bytes
        ) as reads:
            with self.assertRaisesRegex(QualificationCaptureError, "byte-limit"):
                capture_qualification_source_records(
                    QualificationEvidenceRecorder(max_bytes=1, max_records=1000),
                    candidate=candidate,
                    cas=self.cas,
                )
            reads.assert_not_called()
        manifest = self._cas_manifest(candidate.source_manifest_identity)
        tree = self.cas.get_manifest(BlobRef.from_dict(manifest["tree_record"]))
        self.cas.path_for(BlobRef.from_dict(tree["files"][0]["blob"])).unlink()
        with self.assertRaisesRegex(
            QualificationCaptureError, "source-records-invalid"
        ):
            capture_qualification_source_records(
                QualificationEvidenceRecorder(max_bytes=5_000_000, max_records=1000),
                candidate=candidate,
                cas=self.cas,
            )

    def test_retained_source_capture_refuses_rehashed_foreign_manifest(self) -> None:
        candidate = self.runner(self.node).candidate
        manifest = self._cas_manifest(candidate.source_manifest_identity)
        changed = self.cas.put_manifest(
            {
                **manifest,
                "recipe_identity": canonical_identity("foreign-recipe").to_dict(),
            }
        )
        with self.assertRaisesRegex(
            QualificationCaptureError, "source-records-mismatch"
        ):
            capture_qualification_source_records(
                QualificationEvidenceRecorder(max_bytes=5_000_000, max_records=1000),
                candidate=replace(
                    candidate,
                    source_manifest_identity=ContentIdentity.parse_uri(
                        changed.identity
                    ),
                ),
                cas=self.cas,
            )

    def test_source_capture_requires_generation_authority_payloads(self) -> None:
        candidate = self.runner(self.node).candidate
        for identity in (
            self.invocation.identity,
            self.execution_plan.identity,
            canonical_identity(self.invocation.stage_request),
            ContentIdentity.parse_uri(self.execution_plan.route_decisions[-1].digest),
        ):
            path = self.cas.path_for(BlobRef(identity.digest, 0))
            content = path.read_bytes()
            path.unlink()
            try:
                with (
                    self.subTest(missing=identity),
                    self.assertRaisesRegex(
                        QualificationCaptureError, "source-records-invalid"
                    ),
                ):
                    capture_qualification_source_records(
                        QualificationEvidenceRecorder(
                            max_bytes=5_000_000, max_records=1000
                        ),
                        candidate=candidate,
                        cas=self.cas,
                    )
            finally:
                self.cas.put_bytes(content)

    def test_exact_candidate_provenance_and_immutable_records(self) -> None:
        planned_key = self.runner.planned_cache_key(self.node)
        output = self.runner(self.node)
        candidate = output.candidate
        provenance = output.provenance
        request = self.node.request.request
        self.assertEqual(
            candidate.component_revision,
            self.node.plan.component_revision,
        )
        self.assertEqual(candidate.source_generation_request_identity, request.identity)
        self.assertEqual(
            candidate.component_generation_plan_identity,
            self.node.plan.identity,
        )
        self.assertEqual(
            candidate.generation_key_identity,
            self.node.plan.generation_key.identity,
        )
        self.assertEqual(
            candidate.context_manifest_identity,
            request.context_manifest_identity,
        )
        self.assertEqual(candidate.prompt_identity, request.prompt_identity)
        self.assertEqual(candidate.recipe_identity.uri, self.node.recipe.identity)
        self.assertEqual(
            candidate.workspace_allocation_identity,
            self.node.workspace.allocation_identity,
        )
        self.assertEqual(
            provenance.component_lock_identity,
            self.node.recipe.component_lock_identity,
        )
        self.assertEqual(
            provenance.application_root_revision_identity,
            self.application_root,
        )
        self.assertEqual(provenance.readiness_identity, self.readiness)
        self.assertEqual(provenance.candidate_identity, candidate.identity)
        self.assertEqual(len(provenance.route_decision_identities), 1)
        self.assertEqual(len(provenance.model_stage_output_identities), 1)
        self.assertEqual(
            output.runtime_observation.to_dict(),
            {
                "schema": (
                    "urn:literate-ai:schema:v2:component-generation-runtime-observation"
                ),
                "model_attempts": None,
                "wall_time_ms": None,
                "model_tokens": None,
                "cost_microunits": None,
            },
        )
        manifest = self._cas_manifest(candidate.source_manifest_identity)
        tree_reference = BlobRef.from_dict(manifest["tree_record"])
        tree = self.cas.get_manifest(tree_reference)
        self.assertEqual(
            candidate.source_bundle_identity.uri,
            tree_reference.identity,
        )
        self.assertEqual(
            candidate.tree_identity.uri,
            canonical_tree_digest(Path(self.node.workspace.locator)),
        )
        self.assertEqual(tree["tree_identity"], candidate.tree_identity.to_dict())
        self.assertEqual(
            manifest["source_bom"]["bom_identity"],
            candidate.source_bom_identity.to_dict(),
        )
        self.assertEqual(self.generator.accept_invocations, 0)
        self.assertEqual(self.generator.publications, 0)
        self.assertEqual(self.generator.calls[0][4], self.node.request.prompt)
        self.assertEqual(self.runner.cache_key_for_candidate(candidate), planned_key)
        self.assertNotEqual(
            planned_key.request_identity,
            candidate.source_generation_request_identity,
        )

    def test_record_restored_cache_key_makes_a_cross_process_hit_available(
        self,
    ) -> None:
        output = self.runner(self.node)
        candidate = output.candidate
        planned_key = self.runner.planned_cache_key(self.node)

        fresh_runner = CachedCodingCliSourceGenerationRunner(
            _FakeCachedGenerator(),
            cas=self.cas,
            invocation_provider=lambda _node: self.invocation,
        )
        with self.assertRaises(CachedCodingCliSourceGenerationError) as raised:
            fresh_runner.cache_key_for_candidate(candidate)
        self.assertEqual(
            raised.exception.code, "source_generation.cache_key_unavailable"
        )

        fresh_runner.record_restored_cache_key(candidate, planned_key)
        self.assertEqual(fresh_runner.cache_key_for_candidate(candidate), planned_key)

    def test_record_restored_cache_key_rejects_untyped_arguments(self) -> None:
        output = self.runner(self.node)
        candidate = output.candidate
        planned_key = self.runner.planned_cache_key(self.node)
        with self.assertRaises(TypeError):
            self.runner.record_restored_cache_key(object(), planned_key)
        with self.assertRaises(TypeError):
            self.runner.record_restored_cache_key(candidate, object())

    def test_provider_evidence_is_bound_inside_one_stage_output(self) -> None:
        provider_evidence = canonical_identity({"provider": "inherited-session"})
        self.generator.provider_evidence_identity = provider_evidence.uri

        output = self.runner(self.node)

        self.assertEqual(len(output.provenance.route_decision_identities), 1)
        self.assertEqual(len(output.provenance.model_stage_output_identities), 1)
        self.assertEqual(
            output.provenance.provider_evidence_identities,
            (provider_evidence,),
        )
        manifest = self._cas_manifest(output.candidate.source_manifest_identity)
        stage_output = self.cas.get_manifest(
            BlobRef.from_dict(manifest["stage_output_record"])
        )
        self.assertEqual(
            stage_output["provider_evidence_identity"],
            provider_evidence.uri,
        )

    def test_locked_binary_asset_is_merged_after_model_text_generation(self) -> None:
        content = b"\x89PNG\r\n\x1a\nreference-image"
        blob = self.cas.put_bytes(content, media_type="image/png")
        asset = AuthoredBinaryAsset(
            self.node.plan.component_revision,
            "reference-image",
            "source/assets/reference.png",
            "runtime-data",
            canonical_identity({"target": "fixture"}),
            blob,
            self.node.plan.component_graph_identity,
        )
        key = replace(
            self.node.plan.generation_key,
            asset_identities=(asset.identity,),
        )
        plan = replace(self.node.plan, generation_key=key)
        allocation = canonical_identity(
            {
                "schema": "literate-ai/component-workspace-allocation@1",
                "component_revision": plan.component_revision.uri,
                "generation_plan_identity": plan.identity.uri,
                "locator": self.node.workspace.locator,
            }
        )
        workspace = replace(
            self.node.workspace,
            generation_plan_identity=plan.identity,
            generation_key_identity=key.identity,
            allocation_identity=allocation,
        )
        node = replace(self.node, plan=plan, workspace=workspace)
        runner = CachedCodingCliSourceGenerationRunner(
            self.generator,
            cas=self.cas,
            invocation_provider=lambda _node: self.invocation,
            assets=(asset,),
        )

        output = runner(node)
        tree = self._cas_manifest(output.candidate.source_bundle_identity)
        record = next(
            item
            for item in tree["files"]
            if item["path"] == "source/assets/reference.png"
        )

        self.assertEqual(BlobRef.from_dict(record["blob"]), blob)
        self.assertEqual(self.cas.get_bytes(blob), content)
        self.assertNotIn(asset.path, self.generator._cached.files)
        self.assertEqual(
            Path(node.workspace.locator, *asset.path.split("/")).read_bytes(), content
        )
        self.assertEqual(
            local_generated_source_tree_identity(Path(node.workspace.locator)),
            output.candidate.tree_identity,
        )

    def test_missing_locked_asset_custody_has_a_stable_failure_code(self) -> None:
        content = b"\x89PNG\r\n\x1a\nmissing-reference-image"
        blob = BlobRef(
            hashlib.sha256(content).hexdigest(),
            len(content),
            media_type="image/png",
        )
        asset = AuthoredBinaryAsset(
            self.node.plan.component_revision,
            "reference-image",
            "source/assets/reference.png",
            "runtime-data",
            canonical_identity({"target": "missing-fixture"}),
            blob,
            self.node.plan.component_graph_identity,
        )
        key = replace(
            self.node.plan.generation_key,
            asset_identities=(asset.identity,),
        )
        plan = replace(self.node.plan, generation_key=key)
        allocation = canonical_identity(
            {
                "schema": "literate-ai/component-workspace-allocation@1",
                "component_revision": plan.component_revision.uri,
                "generation_plan_identity": plan.identity.uri,
                "locator": self.node.workspace.locator,
            }
        )
        node = replace(
            self.node,
            plan=plan,
            workspace=replace(
                self.node.workspace,
                generation_plan_identity=plan.identity,
                generation_key_identity=key.identity,
                allocation_identity=allocation,
            ),
        )
        runner = CachedCodingCliSourceGenerationRunner(
            self.generator,
            cas=self.cas,
            invocation_provider=lambda _node: self.invocation,
            assets=(asset,),
        )

        with self.assertRaises(CachedCodingCliSourceGenerationError) as raised:
            runner(node)

        self.assertEqual(
            raised.exception.code, "source_generation.asset_blob_unavailable"
        )
        self.assertFalse(Path(node.workspace.locator, *asset.path.split("/")).exists())

    def test_cache_replay_returns_exactly_equivalent_source_output(self) -> None:
        first = self.runner(self.node)
        workspace = Path(self.node.workspace.locator)
        for child in workspace.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        second = self.runner(self.node)
        self.assertEqual(self.generator.delegate_calls, 1)
        self.assertEqual(len(self.generator.calls), 2)
        self.assertEqual(first, second)
        self.assertEqual(first.identity, second.identity)
        self.assertEqual(self.generator.accept_invocations, 0)
        self.assertEqual(self.generator.publications, 0)

    def test_runner_integrates_with_source_only_scheduler(self) -> None:
        from literate_ai.application import execute_component_source_generation_node

        result = execute_component_source_generation_node(
            self.node,
            candidate=None,
            explicitly_invalid=False,
            runner=self.runner,
        )
        self.assertEqual(
            result.result.disposition,
            SourceGenerationDisposition.GENERATED,
        )
        assert result.output is not None
        self.assertEqual(
            result.result.candidate_identity,
            result.output.candidate_identity,
        )

    def _fresh_node(self):
        root = self.root / f"fresh-{len(self.generator.calls)}"
        root.mkdir()
        return replace(
            self.node,
            workspace=replace(
                self.node.workspace,
                locator=str(root),
                allocation_identity=canonical_identity(
                    {
                        "schema": "literate-ai/component-workspace-allocation@1",
                        "component_revision": self.node.plan.component_revision.uri,
                        "generation_plan_identity": self.node.plan.identity.uri,
                        "locator": str(root.resolve(strict=True)),
                    }
                ),
            ),
        )

    def test_filesystem_workspace_must_be_exact_existing_fresh_allocation(self) -> None:
        base = self.root
        cases = []
        relative = replace(self.node.workspace, locator="relative-workspace")
        cases.append((relative, "source_generation.workspace_not_absolute"))
        missing_path = base / "missing"
        missing = replace(self.node.workspace, locator=str(missing_path))
        cases.append((missing, "source_generation.workspace_missing"))
        file_path = base / "not-directory"
        file_path.write_text("not a directory", encoding="utf-8")
        file_workspace = replace(self.node.workspace, locator=str(file_path))
        cases.append((file_workspace, "source_generation.workspace_not_directory"))
        nonempty_path = base / "nonempty"
        nonempty_path.mkdir()
        (nonempty_path / "content").write_text("occupied", encoding="utf-8")
        nonempty = replace(self.node.workspace, locator=str(nonempty_path))
        cases.append((nonempty, "source_generation.workspace_not_empty"))
        wrong_allocation_path = base / "wrong-allocation"
        wrong_allocation_path.mkdir()
        wrong_allocation = replace(
            self.node.workspace,
            locator=str(wrong_allocation_path),
        )
        cases.append(
            (wrong_allocation, "source_generation.workspace_allocation_mismatch")
        )
        for workspace, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(CachedCodingCliSourceGenerationError) as raised:
                    self.runner(replace(self.node, workspace=workspace))
                self.assertEqual(raised.exception.code, code)

        target = base / "workspace-target"
        target.mkdir()
        link = base / "workspace-link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            return
        linked = replace(self.node.workspace, locator=str(link))
        with self.assertRaises(CachedCodingCliSourceGenerationError) as raised:
            self.runner(replace(self.node, workspace=linked))
        self.assertEqual(
            raised.exception.code,
            "source_generation.workspace_redirected",
        )

    def test_invalid_test_manifest_is_rejected_before_candidate_creation(self) -> None:
        self.generator._cached = self.generator.generate(
            self.node.recipe,
            output_root=Path(self.node.workspace.locator),
            execution_plan=self.execution_plan,
            stage_request=self.stage_request,
            bounded_prompt=self.node.request.prompt,
        )
        bad = replace(
            self.generator._cached,
            files={
                **self.generator._cached.files,
                GENERATED_TEST_SUITE_PATH: "{}",
            },
        )
        self.generator._cached = bad
        with self.assertRaises(GeneratedTestSuiteError):
            self.runner(self._fresh_node())

    def test_bridge_and_runner_expose_no_post_source_ports(self) -> None:
        self.runner(self.node)
        self.assertEqual(
            tuple(inspect.signature(self.runner.__call__).parameters),
            ("prepared",),
        )
        self.assertFalse(
            {
                "build",
                "index",
                "authorize",
                "test",
                "execute",
                "accept",
                "publish",
                "admit",
            }
            & set(vars(self.runner))
        )
        for reference in self.cas.iter_refs():
            if reference.media_type.endswith("+json"):
                document = self.cas.get_manifest(reference)
                encoded = json.dumps(document, sort_keys=True)
                for forbidden in (
                    "authorization_identity",
                    "build_request",
                    "acceptance_identity",
                    "test_result",
                ):
                    self.assertNotIn(forbidden, encoded)
        self.assertEqual(self.generator.accept_invocations, 0)
        self.assertEqual(self.generator.publications, 0)

    def test_cache_publication_requires_the_exact_accepted_candidate(self) -> None:
        output = self.runner(self.node)
        self.assertEqual(self.generator.publications, 0)
        substituted = replace(
            output.candidate,
            tree_identity=canonical_identity({"tree": "substituted"}),
            source_bundle_identity=canonical_identity({"tree": "substituted"}),
        )
        with self.assertRaises(CachedCodingCliSourceGenerationError) as raised:
            self.runner.publish_accepted_candidate(substituted)
        self.assertEqual(
            raised.exception.code,
            "source_generation.candidate_not_pending",
        )
        self.assertEqual(self.generator.publications, 0)

        self.runner.publish_accepted_candidate(output.candidate)
        self.assertEqual(self.generator.accept_invocations, 1)
        self.assertEqual(self.generator.publications, 1)
        with self.assertRaises(CachedCodingCliSourceGenerationError):
            self.runner.publish_accepted_candidate(output.candidate)

        replay = self.runner(self._fresh_node())
        self.runner.publish_accepted_candidate(replay.candidate)
        self.assertEqual(self.generator.accept_invocations, 2)
        self.assertEqual(self.generator.publications, 1)

    def test_standard_membership_publishes_only_its_exact_pending_candidate(
        self,
    ) -> None:
        output = self.runner(self.node)
        request = self.node.request.request
        membership = StandardSourceCacheMembership(
            self.node.plan.component_revision,
            self.node.plan.generation_key.identity,
            SourceGenerationResumeCandidate(
                output,
                output.identity,
                request.budget.identity,
                request.complexity_decision_identity,
            ),
            canonical_identity({"acceptance": "current"}),
        )

        self.assertEqual(self.runner.publish(membership), membership.identity)
        self.assertEqual(self.generator.accept_invocations, 1)
        self.assertEqual(self.generator.publications, 1)
        with self.assertRaises(CachedCodingCliSourceGenerationError):
            self.runner.publish(membership)

    def test_nonfinal_or_foreign_execution_bridge_fails_closed(self) -> None:
        nonfinal = {
            **self.stage_request,
            "stage_id": self.execution_plan.model_stages[0].stage_id,
        }
        with self.assertRaises(CachedCodingCliSourceGenerationError) as raised:
            CodingCliSourceGenerationInvocation.create(
                self.execution_plan,
                nonfinal,
                application_root_revision_identity=self.application_root,
                readiness_identity=self.readiness,
            )
        self.assertEqual(
            raised.exception.code,
            "source_generation.stage_request_not_final",
        )

        foreign_plan = replace(
            self.execution_plan,
            workflow_reference=replace(
                self.execution_plan.workflow_reference,
                identity=canonical_identity({"workflow": "foreign"}),
            ),
        )
        foreign = CodingCliSourceGenerationInvocation.create(
            foreign_plan,
            self.stage_request,
            application_root_revision_identity=self.application_root,
            readiness_identity=self.readiness,
        )
        runner = CachedCodingCliSourceGenerationRunner(
            self.generator,
            cas=self.cas,
            invocation_provider=lambda _node: foreign,
        )
        with self.assertRaises(CachedCodingCliSourceGenerationError) as raised:
            runner(self.node)
        self.assertEqual(
            raised.exception.code,
            "source_generation.execution_plan_authority_mismatch",
        )

    def test_indexing_enabled_delegate_is_rejected(self) -> None:
        generator = _FakeCachedGenerator()
        generator.delegate.source_intelligence_mode = "required"
        generator.delegate.source_intelligence_provider = object()
        with self.assertRaises(CachedCodingCliSourceGenerationError) as raised:
            CachedCodingCliSourceGenerationRunner(
                generator,
                cas=self.cas,
                invocation_provider=lambda _node: self.invocation,
            )
        self.assertEqual(raised.exception.code, "source_generation.indexing_enabled")


if __name__ == "__main__":
    unittest.main()
