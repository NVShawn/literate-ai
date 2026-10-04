"""Integration tests for the reusable standard Component lifecycle service."""

from __future__ import annotations

import hashlib
import json
import threading
import unittest
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from literate_ai.application.artifact_graph import (
    create_artifact_build_graph,
    create_composite_build_request,
    create_package_plan,
    realize_manifest,
)
from literate_ai.application.component_generation_preparation import (
    ComponentGenerationWorkspaceDescriptor,
    PreparedComponentGenerationNode,
)
from literate_ai.application.standard_project_lifecycle import (
    StandardBuildAuthorization,
    StandardBuildOutput,
    StandardComponentBuildIntent,
    StandardComponentBuildPlan,
    StandardNodeAcceptedCandidate,
    StandardProjectLifecycleService,
    StandardSourceCacheMembership,
)
from literate_ai.contracts import (
    StandardLifecycleStage,
    StandardNodeFailurePhase,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.contracts.executable_components import (
    ArtifactExport,
    ArtifactMaterializationPlan,
    BuildActionRequest,
    BuildPrivilege,
    BuildSubActionKind,
    ComponentBuildManifest,
    ContextCacheOutcome,
    ForwardGenerationContextCacheReport,
    GeneratedSourceCandidate,
    PackagedFile,
    PackageEntrypoint,
    PackageKind,
    PackageResult,
    SourceGenerationDisposition,
    SourceGenerationProvenance,
    SourceGenerationResumeCandidate,
    SourceGenerationRunOutput,
    create_component_context_benchmark_record,
    create_forward_generation_context_cache_entry,
    create_forward_generation_prompt_journal,
)
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.security import BuildAuthorization, BuildRequest, SecurityProfile
from tests.support.fixtures_test_component_execution_planning import (
    _diamond_lock,
)
from tests.support.fixtures_test_component_generation_scheduling import (
    _decision,
    _names,
    _prepared_execution,
)


def _identity(label: str):
    return canonical_identity({"standard-lifecycle": label})


def _source_record(name: str) -> tuple[ContentIdentity, BlobRef, bytes, BlobRef, bytes]:
    tree_identity = canonical_identity({"generated": name})
    file_bytes = f"generated-source:{name}".encode()
    file_blob = BlobRef(
        hashlib.sha256(file_bytes).hexdigest(),
        len(file_bytes),
        media_type="text/plain",
    )
    record_bytes = canonical_json_bytes(
        {
            "schema": "literate-ai/generated-source-tree-record@1",
            "tree_identity": tree_identity.to_dict(),
            "files": [{"path": "main.txt", "blob": file_blob.to_dict()}],
        }
    )
    record_blob = BlobRef(
        hashlib.sha256(record_bytes).hexdigest(),
        len(record_bytes),
        media_type="application/vnd.literate-ai.generated-source-tree+json",
    )
    return tree_identity, file_blob, file_bytes, record_blob, record_bytes


def command_contract_fixture(
    revision,
    name,
    *,
    producer_label="baseline",
    library_exports=False,
    artifact_target_identity=None,
):
    from literate_ai.contracts.executable_components import (
        ComponentArtifactExportShape,
        ComponentCommandContract,
        ComponentCommandPhase,
        ComponentCommandToolBinding,
        ComponentLifecycleCommand,
    )
    from tests.support.fixtures_test_library_products import library_product

    compiler = _identity(f"compiler-{producer_label}")
    return ComponentCommandContract(
        revision,
        _identity("fixture-build-authority"),
        _identity("resolver"),
        _identity("build-driver"),
        compiler,
        _identity("runtime"),
        (
            ComponentLifecycleCommand(
                ComponentCommandPhase.BUILD,
                (
                    "{tool}",
                    "fixture-build",
                    "{source_root}",
                    "{export_path}",
                    "{object_root}",
                    "{provider_artifacts}",
                ),
            ),
            ComponentLifecycleCommand(
                ComponentCommandPhase.TEST,
                ("{tool}", "fixture-test", "{artifact_root}"),
            ),
            ComponentLifecycleCommand(
                ComponentCommandPhase.EXECUTE,
                ("{tool}", "fixture-execute", "{artifact_root}"),
            ),
        ),
        tuple(
            ComponentCommandToolBinding(phase, compiler)
            for phase in ComponentCommandPhase
        ),
        ComponentArtifactExportShape(
            f"artifact-{name}",
            "library"
            if library_exports
            else "executable"
            if name == "invoice-cli"
            else "static-library",
            _identity("abi"),
            artifact_target_identity or _identity("target"),
            "application/x-native",
            compiler,
        ),
        library_import_surface=(
            library_product("rust").import_surface if library_exports else None
        ),
    )


@dataclass(frozen=True)
class _Recipe:
    identity: object
    component_lock_identity: object


def _prepared_nodes(execution, requests):
    return {
        plan.component_revision.uri: PreparedComponentGenerationNode(
            plan,
            {"component": plan.component_revision.uri},
            _Recipe(
                _identity(f"recipe-{plan.component_revision.uri}"),
                execution.component_lock_identity,
            ),
            requests[plan.component_revision.uri],
            ComponentGenerationWorkspaceDescriptor(
                plan.component_revision,
                plan.identity,
                plan.generation_key.identity,
                _identity(f"workspace-{plan.component_revision.uri}"),
                f"fixture://{plan.component_revision.uri}",
            ),
        )
        for plan in execution.generation_plans
    }


class LifecyclePorts:
    def __init__(
        self,
        execution,
        names,
        *,
        fail_build=(),
        authorization_label="authorization",
        changed_exports=(),
        export_label="baseline",
        runtime_observations=None,
    ):
        self.execution = execution
        self.names = names
        self.fail_build = set(fail_build)
        self.authorization_label = authorization_label
        self.changed_exports = set(changed_exports)
        self.export_label = export_label
        self.runtime_observations = (
            {} if runtime_observations is None else dict(runtime_observations)
        )
        self.events: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self.plans = {}
        self.planned_sources = {}
        self.realized_exports = {}
        self.intent_artifacts = {}
        self.intent_package_artifacts = {}
        self.generated_outputs = {}
        self.published_memberships = {}
        self.issued_receipts = []

    def _record(self, stage, component="project"):
        with self._lock:
            self.events.append((stage, component))

    def validate(self, execution_plan):
        self._record("validate")
        return _identity("validation")

    def create(
        self,
        execution_plan,
        generation_plan,
        source_candidate,
        provider_artifacts,
        package_artifacts,
    ):
        revision = generation_plan.component_revision
        name = self.names[revision.uri]
        self._record("intent", name)
        source_tree_identity = source_candidate.tree_identity
        self.planned_sources[name] = source_tree_identity
        producer_label = (
            self.export_label if name in self.changed_exports else "baseline"
        )
        intent = StandardComponentBuildIntent(
            revision,
            source_tree_identity,
            source_candidate.source_bundle_identity,
            BuildRequest(
                effective_revision_digest=revision.uri,
                source_bundle_digest=source_candidate.source_bundle_identity.uri,
                builder_id=_identity("fixture-build-authority").uri,
                toolchain_digest=_identity(f"compiler-{producer_label}").uri,
                sandbox_profile="local-explicit-host-process",
                requested_privileges=("execute-build-tools",),
                allowed_outputs=(f"artifact-{name}",),
            ),
            tuple(item.identity for item in provider_artifacts),
            tuple(item.identity for item in package_artifacts),
        )
        self.intent_artifacts[intent.identity.uri] = provider_artifacts
        self.intent_package_artifacts[intent.identity.uri] = package_artifacts
        return intent

    def finalize(self, intent, authorization):
        revision = intent.component_revision
        source_tree_identity = intent.source_tree_identity
        provider_artifacts = self.intent_artifacts[intent.identity.uri]
        package_artifacts = self.intent_package_artifacts[intent.identity.uri]
        name = self.names[revision.uri]
        self._record("finalize", name)
        payload = getattr(self, "artifact_payloads", {}).get(name, name.encode())
        producer_label = (
            self.export_label if name in self.changed_exports else "baseline"
        )
        producer = _identity(f"compiler-{producer_label}")
        export = ArtifactExport(
            export_id=f"artifact-{name}",
            component_revision=revision,
            role=(
                "library"
                if getattr(self, "library_exports", False)
                else "executable"
                if name == "invoice-cli"
                else "static-library"
            ),
            abi_identity=_identity("abi"),
            target_identity=getattr(
                self, "artifact_target_identity", _identity("target")
            ),
            media_type="application/x-native",
            producer_identity=producer,
            source_tree_identity=source_tree_identity,
            toolchain_identity=producer,
            authorization_identity=authorization.authorization_identity,
            dependency_artifact_identities=tuple(
                sorted(
                    {
                        item.identity.uri: item.identity
                        for item in (*provider_artifacts, *package_artifacts)
                    }.values(),
                    key=lambda item: item.uri,
                )
            ),
            blob=BlobRef(
                hashlib.sha256(payload).hexdigest(),
                len(payload),
                media_type="application/x-native",
            ),
        )
        command_contract = command_contract_fixture(
            revision,
            name,
            producer_label=producer_label,
            library_exports=getattr(self, "library_exports", False),
            artifact_target_identity=getattr(self, "artifact_target_identity", None),
        )
        action = BuildActionRequest(
            action_id=f"build-{command_contract.identity.digest[:24]}",
            component_revision=revision,
            role=export.role,
            abi_identity=export.abi_identity,
            target_identity=export.target_identity,
            media_type=export.media_type,
            producer_identity=export.producer_identity,
            source_tree_identity=source_tree_identity,
            toolchain_identity=export.toolchain_identity,
            authorization_identity=export.authorization_identity,
            dependency_artifacts=provider_artifacts,
            package_dependency_artifacts=package_artifacts,
            declared_output_ids=(export.export_id,),
        )
        manifest = ComponentBuildManifest(
            revision,
            source_tree_identity,
            _identity("build-driver"),
            (action,),
            (),
            (export.declaration,),
        )
        materialization = ArtifactMaterializationPlan(
            source_tree_identity, _identity(f"execution-root-{name}"), ()
        )
        request = create_composite_build_request(
            manifest,
            materialization,
            build_system_resolver_identity=_identity("resolver"),
            language_compiler_identity=export.toolchain_identity,
            language_runtime_identity=_identity("runtime"),
            ordered_actions=((BuildSubActionKind.COMPILE, action.action_id),),
            requested_privileges=(BuildPrivilege.EXECUTE_BUILD_TOOLS,),
        )
        plan = StandardComponentBuildPlan(
            revision,
            manifest,
            materialization,
            request,
            tuple(item.identity for item in provider_artifacts),
            tuple(item.identity for item in package_artifacts),
        )
        self.plans[revision.uri] = plan
        self.realized_exports[revision.uri] = (export,)
        return plan

    def __call__(self, prepared):
        plan = prepared.plan
        request = prepared.request.request
        name = self.names[plan.component_revision.uri]
        self._record("generate", name)
        tree_identity, _file_blob, _file_bytes, record_blob, _record_bytes = (
            _source_record(name)
        )
        suite_content = getattr(self, "generated_suites", {}).get(
            plan.component_revision
        )
        suite_identity = (
            self.process_records.remember_bytes(suite_content)
            if suite_content is not None
            else _identity(f"generated-tests-{name}")
        )
        boms = getattr(self, "generated_boms", {}).get(plan.component_revision)
        source_bom_identity = (
            self.process_records.remember_bytes(boms[0])
            if boms is not None
            else _identity(f"source-bom-{name}")
        )
        candidate = GeneratedSourceCandidate(
            plan.component_revision,
            request.identity,
            _identity(f"planned-coding-cli-request-{name}"),
            plan.identity,
            plan.generation_key.identity,
            request.context_manifest_identity,
            request.prompt_identity,
            prepared.recipe.identity,
            prepared.workspace.allocation_identity,
            tree_identity,
            ContentIdentity.parse_uri(record_blob.identity),
            _identity(f"source-manifest-{name}"),
            source_bom_identity,
            suite_identity,
        )
        stage_identity = _identity(f"model-output-{name}")
        route_identity = _identity(f"route-{name}")
        if boms is not None and suite_content is not None:
            from literate_ai.contracts import CYCLONEDX_SOURCE_SBOM_PATH
            from literate_ai.generated_tests import GENERATED_TEST_SUITE_PATH

            records = json.loads(_record_bytes)["files"]
            self.process_records.remember_bytes(_file_bytes)
            for path, content in (
                (CYCLONEDX_SOURCE_SBOM_PATH, boms[0]),
                (GENERATED_TEST_SUITE_PATH, suite_content),
            ):
                reference = BlobRef(
                    self.process_records.remember_bytes(content).digest, len(content)
                )
                records.append({"path": path, "blob": reference.to_dict()})
            records.sort(key=lambda item: item["path"])
            tree_identity = canonical_identity(
                [
                    {
                        "path": item["path"],
                        "size": item["blob"]["size"],
                        "digest": BlobRef.from_dict(item["blob"]).identity,
                    }
                    for item in records
                ]
            )
            tree_bytes = canonical_json_bytes(
                {
                    "schema": "literate-ai/generated-source-tree-record@1",
                    "tree_identity": tree_identity.to_dict(),
                    "files": records,
                }
            )
            tree_reference = BlobRef(
                self.process_records.remember_bytes(tree_bytes).digest, len(tree_bytes)
            )
            candidate = replace(
                candidate,
                tree_identity=tree_identity,
                source_bundle_identity=ContentIdentity.parse_uri(
                    tree_reference.identity
                ),
            )
            from literate_ai.adapters.source_generation import (
                CodingCliSourceGenerationInvocation,
            )

            model_plan = self.generation_execution_plan
            final_stage = model_plan.model_stages[-1]
            final_route = model_plan.route_decisions[-1]
            stage_request = {
                "stage_id": final_stage.stage_id,
                "prior_stage_outputs": {},
                "input_identity": _identity(f"input-{name}").to_dict(),
            }
            invocation = CodingCliSourceGenerationInvocation.create(
                model_plan,
                stage_request,
                application_root_revision_identity=self.execution.root_revision,
                readiness_identity=_identity(f"readiness-{name}"),
            )
            self.process_records.remember_json(invocation.identity_document())
            self.process_records.remember_json(model_plan.to_dict())
            request_identity = self.process_records.remember_json(stage_request)
            route_identity = self.process_records.remember_json(final_route.to_dict())
            stage_bytes = canonical_json_bytes(
                {
                    "schema": "literate-ai/coding-cli-stage-output-record@1",
                    "execution_plan_identity": model_plan.identity.to_dict(),
                    "stage_request_identity": request_identity.to_dict(),
                    "planned_request_identity": (
                        candidate.planned_coding_cli_request_identity.to_dict()
                    ),
                    "stage_id": final_stage.stage_id,
                    "route_decision_identity": route_identity.to_dict(),
                    "tree_identity": tree_identity.to_dict(),
                    "tree_record": tree_reference.to_dict(),
                    "coding_cli": "fixture",
                    "model": None,
                    "coding_cli_tool_binding_identity": None,
                }
            )
            stage_identity = self.process_records.remember_bytes(stage_bytes)
            stage_reference = BlobRef(stage_identity.digest, len(stage_bytes))
            from literate_ai.adapters.dependencies import validate_cyclonedx_bom
            from literate_ai.contracts.sbom import CycloneDxLifecycle

            source_binding = validate_cyclonedx_bom(
                boms[0], lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=boms[2]
            )
            manifest = {
                "schema": "literate-ai/generated-source-manifest-record@1",
                **{
                    key: value
                    for key, value in candidate.to_dict().items()
                    if key
                    not in {
                        "schema",
                        "source_bundle_identity",
                        "source_manifest_identity",
                        "source_bom_identity",
                        "generated_test_suite_identity",
                    }
                },
                "invocation_identity": invocation.identity.to_dict(),
                "tree_record": tree_reference.to_dict(),
                "stage_output_record": stage_reference.to_dict(),
                "source_bom": source_binding.to_dict(),
                "generated_test_suite_identity": suite_identity.uri,
            }
            candidate = replace(
                candidate,
                source_manifest_identity=self.process_records.remember_json(manifest),
            )
        provenance = SourceGenerationProvenance(
            request.identity,
            candidate.planned_coding_cli_request_identity,
            prepared.recipe.component_lock_identity,
            self.execution.root_revision,
            plan.component_revision,
            plan.identity,
            plan.generation_key.identity,
            request.context_manifest_identity,
            request.prompt_identity,
            prepared.recipe.identity,
            prepared.workspace.allocation_identity,
            _identity(f"readiness-{name}"),
            (route_identity,),
            (stage_identity,),
            candidate.identity,
        )
        output = SourceGenerationRunOutput(
            candidate,
            candidate.identity,
            provenance,
            provenance.identity,
            self.runtime_observations.get(name),
        )
        self.generated_outputs[plan.component_revision.uri] = output
        return output

    def index(self, revision, source):
        name = self.names[revision.uri]
        self._record("index", name)
        return _identity(f"index-{name}")

    def authorize(self, intent, index):
        name = self.names[intent.component_revision.uri]
        self._record("authorize", name)
        issued = datetime(2026, 8, 7, tzinfo=UTC)
        return StandardBuildAuthorization(
            intent.identity,
            intent.build_request_identity,
            index,
            BuildAuthorization(
                authorization_id=f"fixture:{self.authorization_label}",
                classification_digest=index.uri,
                request_digest=intent.build_request_identity.uri,
                effective_revision_digest=intent.component_revision.uri,
                actor="fixture",
                reason=self.authorization_label,
                profile=SecurityProfile.CONSTRAINED,
                privileges=intent.build_request.requested_privileges,
                issued_at=issued,
                expires_at=issued + timedelta(minutes=10),
            ),
        )

    def build(self, plan, provider_artifacts):
        name = self.names[plan.component_revision.uri]
        self._record("build", name)
        if name in self.fail_build:
            raise RuntimeError("fixture build failure")
        self.assert_provider_artifacts(plan, provider_artifacts)
        return StandardBuildOutput(
            self.realized_exports[plan.component_revision.uri],
            _identity(f"build-{name}"),
        )

    @staticmethod
    def assert_provider_artifacts(plan, provider_artifacts):
        if tuple(item.identity for item in provider_artifacts) != (
            plan.provider_artifact_identities
        ):
            raise AssertionError("provider artifact order or membership changed")

    def test(self, plan, exports):
        name = self.names[plan.component_revision.uri]
        self._record("test", name)
        return _identity(f"test-{name}")

    def execute(self, plan, exports):
        name = self.names[plan.component_revision.uri]
        self._record("execute", name)
        return _identity(f"execute-{name}")

    def accept(self, plan, test_identity, execution_identity):
        name = self.names[plan.component_revision.uri]
        self._record("accept", name)
        return _identity(f"accept-{name}")

    def publish(self, membership):
        name = self.names[membership.component_revision.uri]
        self._record("publish", name)
        self.published_memberships[membership.component_revision.uri] = membership
        return membership.identity

    def assemble_project_artifacts(
        self, component_lock, execution_plan, project_build_plan, results
    ):
        self._record("assemble-artifacts")
        manifests = tuple(
            realize_manifest(
                project_build_plan.components[index].manifest,
                result.exports,
            )
            for index, result in enumerate(results)
        )
        root_result = next(
            item
            for item in results
            if item.component_revision == execution_plan.root_revision
        )
        root_export = root_result.exports[0]
        from literate_ai.application.release_artifacts import (
            plan_standard_assembly_dependencies,
        )

        graph = create_artifact_build_graph(
            build_system_driver_identity=manifests[0].build_system_driver_identity,
            manifests=manifests,
            link_roots=(root_export.identity,),
            assembly_dependencies=plan_standard_assembly_dependencies(
                execution_plan, results
            ),
        )
        return graph, graph.link_plans[0]

    def create_project_package(
        self,
        component_lock,
        execution_plan,
        project_build_plan,
        artifact_graph,
        link_plan,
    ):
        self._record("create-package")
        exports = {
            item.identity.uri: item
            for manifest in artifact_graph.manifests
            for item in manifest.exports
        }
        destinations = {
            uri: (
                "bin/app"
                if uri == link_plan.root_artifact_identity.uri
                else f"lib/{item.export_id}.bin"
            )
            for uri in (
                identity.uri for identity in link_plan.ordered_artifact_identities
            )
            for item in (exports[uri],)
        }
        plan = create_package_plan(
            artifact_graph,
            root_component_revision=component_lock.root_revision,
            component_lock_identity=component_lock.identity,
            target_identity=exports[
                link_plan.root_artifact_identity.uri
            ].target_identity,
            root_artifact_identity=link_plan.root_artifact_identity,
            package_kind=(
                PackageKind.DIRECTORY
                if getattr(self, "library_exports", False)
                else PackageKind.STANDALONE_EXECUTABLE
            ),
            packager_identity=_identity("project-packager"),
            destinations=destinations,
            entrypoints=()
            if getattr(self, "library_exports", False)
            else (
                PackageEntrypoint(
                    "app",
                    "application",
                    "bin/app",
                    link_plan.root_artifact_identity,
                ),
            ),
            runtime_requirements=(),
        )
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
        return plan, PackageResult(
            plan.identity,
            plan.root_component_revision,
            plan.component_lock_identity,
            plan.target_identity,
            plan.artifact_graph_identity,
            plan.package_kind,
            plan.packager_identity,
            files,
            files,
            plan.entrypoints,
            plan.runtime_requirements,
        )

    def test_root_integration(
        self, component_lock, execution_plan, project_build_plan, plan, result
    ):
        self._record("root-integration-test")
        return _identity("root-integration-test")

    def execute_packaged_project(
        self, component_lock, execution_plan, project_build_plan, plan, result
    ):
        self._record("execute-package")
        return _identity("execute-package")

    def accept_project_independently(
        self,
        component_lock,
        execution_plan,
        project_build_plan,
        plan,
        result,
        root_test_identity,
        execution_identity,
    ):
        self._record("independent-accept")
        return _identity("independent-accept")

    def admit(self, results):
        self._record("admit")
        return _identity("admission")

    def issue(self, receipt):
        self._record("receipt")
        self.issued_receipts.append(receipt)
        return receipt.identity


class ExpiredAuthorizationPorts(LifecyclePorts):
    def authorize(self, intent, index):
        authorization = super().authorize(intent, index)
        return replace(
            authorization,
            grant=replace(
                authorization.grant,
                issued_at=datetime(2026, 8, 5, tzinfo=UTC),
                expires_at=datetime(2026, 8, 6, tzinfo=UTC),
            ),
        )


class _CheckpointRecorder:
    def __init__(self):
        self.evidence = []

    def record(self, evidence):
        self.evidence.append(evidence)


class _ContextEvidenceRecorder:
    """In-memory unit seam with the same exact projections as the durable adapter."""

    def __init__(self):
        self._records = {}

    def record(self, prepared, result):
        journal = create_forward_generation_prompt_journal(
            prepared.request.request, result
        )
        benchmark = create_component_context_benchmark_record(journal, result)
        outcome = {
            SourceGenerationDisposition.REUSED: ContextCacheOutcome.HIT,
            SourceGenerationDisposition.GENERATED: ContextCacheOutcome.MISS,
            SourceGenerationDisposition.RETAINED: ContextCacheOutcome.BYPASS,
            SourceGenerationDisposition.FAILED: ContextCacheOutcome.BYPASS,
            SourceGenerationDisposition.CANCELLED: ContextCacheOutcome.BYPASS,
        }[result.disposition]
        entry = create_forward_generation_context_cache_entry(
            journal,
            cache_key_identity=prepared.request.request.generation_key_identity,
            outcome=outcome,
        )
        self._records[result.component_revision.uri] = (journal, benchmark, entry)

    @property
    def prompt_journal_identities(self):
        return tuple(self._records[key][0].identity for key in sorted(self._records))

    @property
    def benchmark_records(self):
        return tuple(self._records[key][1] for key in sorted(self._records))

    def cache_report(self):
        return ForwardGenerationContextCacheReport(
            tuple(self._records[key][2] for key in sorted(self._records))
        )


_DEFAULT_CONTEXT_RECORDER = object()


def _service(
    ports,
    *,
    checkpoint_recorder=None,
    context_evidence_recorder=_DEFAULT_CONTEXT_RECORDER,
    candidate_repair_port=None,
):
    return StandardProjectLifecycleService(
        validator=ports,
        build_intent_factory=ports,
        build_plan_finalizer=ports,
        generator=ports,
        indexer=ports,
        authorizer=ports,
        builder=ports,
        tester=ports,
        executor=ports,
        acceptor=ports,
        source_cache_publisher=ports,
        artifact_assembler=ports,
        package_creator=ports,
        root_integration_tester=ports,
        packaged_project_executor=ports,
        independent_project_acceptor=ports,
        admitter=ports,
        receipt_issuer=ports,
        checkpoint_recorder=checkpoint_recorder,
        context_evidence_recorder=(
            _ContextEvidenceRecorder()
            if context_evidence_recorder is _DEFAULT_CONTEXT_RECORDER
            else context_evidence_recorder
        ),
        candidate_repair_port=candidate_repair_port,
        clock=lambda: datetime(2026, 8, 7, 0, 1, tzinfo=UTC),
    )


def _accepted(plan, prepared, name, exports, output):
    request = prepared.request
    generation = SourceGenerationResumeCandidate(
        output,
        output.identity,
        request.request.budget.identity,
        request.request.complexity_decision_identity,
    )
    acceptance = _identity(f"accept-{name}")
    return StandardNodeAcceptedCandidate(
        generation,
        plan.identity,
        exports,
        _identity(f"index-{name}"),
        plan.request.authorization_identity,
        _identity(f"build-{name}"),
        _identity(f"test-{name}"),
        _identity(f"execute-{name}"),
        acceptance,
        StandardSourceCacheMembership(
            plan.component_revision,
            prepared.plan.generation_key.identity,
            generation,
            acceptance,
        ),
    )


class StandardProjectLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._configure_dependencies(DependencyKind.GENERATION)

    def _configure_dependencies(self, kind):
        self.lock = _diamond_lock(dependency_kind=kind)
        self.execution, self.requests = _prepared_execution(self.lock)
        self.nodes = _prepared_nodes(self.execution, self.requests)
        self.names = _names(self.lock)

    def _accepted_baseline(self):
        ports = LifecyclePorts(self.execution, self.names)
        result = _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution,
                self.names,
                "money",
                tuple(self.names.values()),
            ),
            prepared_nodes=self.nodes,
            max_parallelism=2,
        )
        self.assertTrue(result.successful)
        by_uri = {item.component_revision.uri: item for item in result.node_results}
        return {
            uri: _accepted(
                ports.plans[uri],
                self.nodes[uri],
                name,
                ports.realized_exports[uri],
                by_uri[uri].source_output,
            )
            for uri, name in self.names.items()
        }

    def test_every_root_boundary_failure_prevents_cache_publication(self):
        cases = (
            ("assemble_project_artifacts", "assemble-artifacts"),
            ("create_project_package", "create-package"),
            ("test_root_integration", "root-integration-test"),
            ("execute_packaged_project", "execute-package"),
            ("accept_project_independently", "independent-accept"),
        )
        for method_name, stage in cases:
            with self.subTest(stage=stage):
                ports = LifecyclePorts(self.execution, self.names)
                recorder = _CheckpointRecorder()

                def fail(*_args, _stage=stage, _ports=ports, **_kwargs):
                    _ports._record(_stage)
                    raise RuntimeError(f"{_stage} failed")

                setattr(ports, method_name, fail)
                with self.assertRaisesRegex(RuntimeError, f"{stage} failed"):
                    _service(ports, checkpoint_recorder=recorder).execute(
                        self.execution,
                        component_lock=self.lock,
                        invalidation=_decision(
                            self.execution,
                            self.names,
                            "money",
                            tuple(self.names.values()),
                        ),
                        prepared_nodes=self.nodes,
                        max_parallelism=2,
                    )
                self.assertEqual(ports.published_memberships, {})
                self.assertFalse(
                    any(event == "publish" for event, _component in ports.events)
                )
                self.assertFalse(
                    any(
                        item.stage is StandardLifecycleStage.SOURCE_CACHE_PUBLICATION
                        for item in recorder.evidence
                    )
                )
                self.assertNotIn(("admit", "project"), ports.events)
                self.assertEqual(ports.issued_receipts, [])

    def test_expired_grant_fails_before_finalization(self):
        ports = ExpiredAuthorizationPorts(self.execution, self.names)
        result = _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution,
                self.names,
                "money",
                tuple(self.names.values()),
            ),
            prepared_nodes=self.nodes,
            max_parallelism=2,
        )

        self.assertFalse(result.successful)
        self.assertFalse(any(stage == "finalize" for stage, _ in ports.events))
        self.assertFalse(any(stage == "build" for stage, _ in ports.events))
        self.assertIn(
            StandardNodeFailurePhase.BUILD_AUTHORIZATION,
            {
                item.failure_evidence.phase
                for item in result.node_results
                if item.failure_evidence
            },
        )

    def test_failing_node_cancels_consumer_and_preserves_independent_resume(
        self,
    ):
        self._configure_dependencies(DependencyKind.BUILD)
        baseline = self._accepted_baseline()
        ports = LifecyclePorts(self.execution, self.names, fail_build={"pricing"})
        reporting_uri = next(
            uri for uri, name in self.names.items() if name == "reporting"
        )
        resume = {reporting_uri: baseline[reporting_uri]}
        result = _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution,
                self.names,
                "pricing",
                ("money", "pricing", "invoice-cli"),
            ),
            prepared_nodes=self.nodes,
            resume_candidates=resume,
            max_parallelism=2,
        )
        by_name = {
            self.names[item.component_revision.uri]: item
            for item in result.node_results
        }
        self.assertEqual(
            by_name["reporting"].disposition, SourceGenerationDisposition.REUSED
        )
        self.assertEqual(
            by_name["pricing"].disposition, SourceGenerationDisposition.GENERATED
        )
        self.assertEqual(
            by_name["pricing"].failure_evidence.phase,
            StandardNodeFailurePhase.BUILD,
        )
        self.assertEqual(
            by_name["invoice-cli"].disposition,
            SourceGenerationDisposition.GENERATED,
        )
        self.assertEqual(
            by_name["invoice-cli"].failure_evidence.phase,
            StandardNodeFailurePhase.DEPENDENCY,
        )
        builds = [name for stage, name in ports.events if stage == "build"]
        self.assertNotIn("reporting", builds)
        self.assertNotIn("invoice-cli", builds)
        self.assertNotIn(("admit", "project"), ports.events)
        self.assertNotIn(("receipt", "project"), ports.events)
        self.assertFalse(result.successful)

    def test_second_unchanged_run_reuses_every_accepted_node(self):
        resume = self._accepted_baseline()
        ports = LifecyclePorts(self.execution, self.names)
        result = _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(self.execution, self.names, "money"),
            prepared_nodes=self.nodes,
            resume_candidates=resume,
            max_parallelism=2,
        )
        self.assertTrue(result.successful)
        self.assertTrue(
            all(
                item.disposition is SourceGenerationDisposition.REUSED
                for item in result.node_results
            )
        )
        self.assertFalse(
            any(
                stage
                in {
                    "generate",
                    "build",
                    "test",
                    "execute",
                    "accept",
                }
                for stage, _name in ports.events
            )
        )
        self.assertEqual(
            sorted(name for stage, name in ports.events if stage == "index"),
            sorted(self.names.values()),
        )
        self.assertEqual(
            sorted(name for stage, name in ports.events if stage == "authorize"),
            sorted(self.names.values()),
        )
        self.assertFalse(any(stage == "publish" for stage, _ in ports.events))

    def test_rejected_generated_candidate_is_never_published(self):
        ports = LifecyclePorts(self.execution, self.names, fail_build={"pricing"})

        result = _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution,
                self.names,
                "money",
                tuple(self.names.values()),
            ),
            prepared_nodes=self.nodes,
            max_parallelism=2,
        )

        self.assertFalse(result.successful)
        self.assertEqual(
            tuple(
                item.component_revision.uri
                for item in result.lifecycle_membership.cache_decisions
            ),
            tuple(sorted(self.names)),
        )
        self.assertEqual(ports.issued_receipts, [])
        self.assertIsNone(result.aggregate_receipt)
        published = {name for stage, name in ports.events if stage == "publish"}
        self.assertEqual(published, set())
        decisions = {
            self.names[item.component_revision.uri]: item
            for item in result.lifecycle_membership.cache_decisions
        }
        self.assertIsNone(decisions["pricing"].publication_identity)
        self.assertIsNone(decisions["pricing"].accepted_membership_identity)
        self.assertIsNone(decisions["invoice-cli"].publication_identity)
        self.assertTrue(
            any(
                decision.accepted_membership_identity is not None
                and decision.publication_identity is None
                for decision in decisions.values()
            )
        )


if __name__ == "__main__":
    unittest.main()
