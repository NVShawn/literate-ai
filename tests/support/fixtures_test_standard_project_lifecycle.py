"""Shared test fixtures extracted from test_standard_project_lifecycle."""

from __future__ import annotations

import hashlib
import json
import threading
import unittest
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import PropertyMock, patch

from literate_ai.adapters.models.coding_cli import CodingCliError
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
    capture_lifecycle_records,
    reopen_qualification_lifecycle,
    reopen_qualification_root,
    reopen_qualification_run,
)
from literate_ai.application.artifact_graph import (
    create_artifact_build_graph,
    create_composite_build_request,
    create_package_plan,
    realize_manifest,
)
from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.application.component_generation_context import (
    PreparedComponentGenerationRequest,
    prepare_component_generation_context,
)
from literate_ai.application.component_generation_preparation import (
    ComponentGenerationWorkspaceDescriptor,
    PreparedComponentGenerationNode,
)
from literate_ai.application.standard_lifecycle_membership import (
    StandardLifecycleMembershipError,
    assemble_standard_lifecycle_membership,
)
from literate_ai.application.standard_project_lifecycle import (
    StandardBuildAuthorization,
    StandardBuildOutput,
    StandardComponentBuildIntent,
    StandardComponentBuildPlan,
    StandardNodeAcceptedCandidate,
    StandardProjectLifecycleError,
    StandardProjectLifecycleService,
    StandardSourceCacheMembership,
)
from literate_ai.contracts import (
    ComponentLock,
    StandardLifecycleCheckpointOutcome,
    StandardLifecycleStage,
    StandardNodeCacheOutcome,
    StandardNodeFailurePhase,
    StandardSourceAdmissionEvidence,
    StandardSourceAdmissionMembership,
    StandardSourceSelectorScope,
    StandardSourceSelectorSet,
    StandardSourceTestResult,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.contracts.executable_components import (
    ArtifactExport,
    ArtifactMaterializationPlan,
    BuildActionRequest,
    BuildPrivilege,
    BuildSubActionKind,
    CandidateAttemptChainDisposition,
    CandidateFailureClassification,
    CandidateRepairDiagnostic,
    ComponentBuildManifest,
    ComponentGenerationRuntimeObservation,
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
from literate_ai.diagnostics import verbose_diagnostics, verbose_enabled
from literate_ai.security import BuildAuthorization, BuildRequest, SecurityProfile
from tests.support.fixtures_test_component_execution_planning import (
    _diamond_lock,
    _models,
)
from tests.support.fixtures_test_component_generation_context import (
    _budget as _context_budget,
)
from tests.support.fixtures_test_component_generation_context import (
    _materialize,
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


def _prepared_execution_with_budget(lock, budget):
    execution = plan_component_execution(lock, model_identities=_models(lock))
    prepared = {}
    plans = []
    for original in execution.generation_plans:
        plan, segments = _materialize(original)
        request = prepare_component_generation_context(
            plan,
            framework_envelope=b"scheduler envelope\n",
            authority_segments=segments,
            budget=budget,
        )
        plans.append(plan)
        prepared[plan.component_revision.uri] = request
    return replace(execution, generation_plans=tuple(plans)), prepared


def _root_bound_prepared_nodes(execution, requests):
    """Give otherwise shared nodes distinct application-root custody."""

    return {
        plan.component_revision.uri: PreparedComponentGenerationNode(
            plan,
            {"component": plan.component_revision.uri},
            _Recipe(
                canonical_identity(
                    {
                        "schema": "literate-ai/test-root-bound-recipe@1",
                        "component_lock_identity": (
                            execution.component_lock_identity.uri
                        ),
                        "component_revision": plan.component_revision.uri,
                    }
                ),
                execution.component_lock_identity,
            ),
            requests[plan.component_revision.uri],
            ComponentGenerationWorkspaceDescriptor(
                plan.component_revision,
                plan.identity,
                plan.generation_key.identity,
                canonical_identity(
                    {
                        "schema": "literate-ai/test-root-bound-workspace@1",
                        "component_lock_identity": (
                            execution.component_lock_identity.uri
                        ),
                        "component_revision": plan.component_revision.uri,
                    }
                ),
                "fixture://"
                f"{execution.component_lock_identity.digest}/"
                f"{plan.component_revision.digest}",
            ),
        )
        for plan in execution.generation_plans
    }


def _single_component_lock(lock: ComponentLock, name: str) -> ComponentLock:
    node = next(item for item in lock.nodes if item.revision.coordinate.name == name)
    authoring = next(
        item
        for item in lock.authorings
        if item.identity == node.revision.authoring_identity
    )
    return ComponentLock(
        target_name=lock.target_name,
        target_profile_identity=lock.target_profile_identity,
        selection_policy_identity=lock.selection_policy_identity,
        resolver_identity=lock.resolver_identity,
        root_revision=node.revision.identity,
        nodes=(node,),
        edges=(),
        _authorings=(authoring,),
    )


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


class ContractEvidenceLifecyclePorts(LifecyclePorts):
    """Typed stage contract fixture; it does not compile or qualify native source."""

    def __init__(
        self,
        execution,
        names,
        *,
        wrong_source_bom=False,
        library_exports=False,
        root_acceptance=None,
        generated_suites=None,
        generated_boms=None,
    ):
        super().__init__(execution, names)
        self.typed_acceptances = {}
        self.wrong_source_bom = wrong_source_bom
        self.library_exports = library_exports
        self.root_acceptance = root_acceptance
        self.generated_suites = generated_suites or {}
        self.generated_boms = generated_boms or {}
        self.process_records = QualificationEvidenceRecorder(
            max_bytes=5_000_000, max_records=1000
        )

    def create(self, *arguments):
        intent = super().create(*arguments)
        self.process_records.remember_json(intent.to_dict())
        self.process_records.remember_json(intent.build_request.to_dict())
        name = self.names[intent.component_revision.uri]
        contract = command_contract_fixture(
            intent.component_revision,
            name,
            producer_label=self.export_label
            if name in self.changed_exports
            else "baseline",
            library_exports=self.library_exports,
            artifact_target_identity=getattr(self, "artifact_target_identity", None),
        )
        self.process_records.remember_json(contract.to_dict())
        for command in contract.commands:
            self.process_records.remember_json(command.to_dict())
        if not contract.is_library:
            for entrypoint in contract.entrypoint_command_contracts():
                self.process_records.remember_json(entrypoint.to_dict())
                for command in entrypoint.commands:
                    self.process_records.remember_json(command.to_dict())
        return intent

    def index(self, revision, source):
        super().index(revision, source)
        return self.process_records.remember_json(
            {
                "indexer": "local-tree@1",
                "component": revision.uri,
                "tree": source.uri,
                "path_count": 3,
            }
        )

    def authorize(self, intent, index):
        authorization = super().authorize(intent, index)
        self.process_records.remember_json(authorization.grant.to_dict())
        self.process_records.remember_json(authorization.to_dict())
        return authorization

    def test_root_integration(
        self, component_lock, execution_plan, project_build_plan, plan, result
    ):
        super().test_root_integration(
            component_lock, execution_plan, project_build_plan, plan, result
        )
        tests = self.typed_acceptances[plan.root_component_revision.uri].generated_tests
        output = json.dumps(
            {
                "schema": "literate-ai/generated-test-results@1",
                "cases": [
                    {"case_id": case.case_id, "outcome": "passed"}
                    for case in tests.cases
                ],
            }
        )
        return self._packaged_process_record(
            plan, "packaged-root-generated-test", output
        )

    def execute_packaged_project(
        self, component_lock, execution_plan, project_build_plan, plan, result
    ):
        super().execute_packaged_project(
            component_lock, execution_plan, project_build_plan, plan, result
        )
        return self._packaged_process_record(
            plan, "packaged-project-execution", "fixture library smoke output"
        )

    def _packaged_process_record(self, plan, phase, output):
        return self.process_records.remember_json(
            {
                "schema": "literate-ai/local-process-observation@1",
                "phase": phase,
                "plan_identity": plan.identity.uri,
                "returncode": 0,
                "stdout_identity": self.process_records.remember_json(output).uri,
                "stderr_identity": self.process_records.remember_json("").uri,
            }
        )

    def accept_project_independently(self, *arguments):
        baseline = super().accept_project_independently(*arguments)
        if self.root_acceptance is None:
            return baseline
        return self.root_acceptance(*arguments)

    def build(self, plan, provider_artifacts):
        from tests.support.fixtures_test_standard_post_source_evidence import _evidence

        built = super().build(plan, provider_artifacts)
        prototype = _evidence()
        output = self.generated_outputs[plan.component_revision.uri]
        name = self.names[plan.component_revision.uri]
        source_bom = replace(
            prototype.build.source_sbom,
            bom_identity=(
                _identity("foreign-source-bom")
                if self.wrong_source_bom
                else output.candidate.source_bom_identity
            ),
        )
        if plan.component_revision in self.generated_boms:
            from literate_ai.adapters.dependencies import validate_cyclonedx_bom
            from literate_ai.contracts.sbom import CycloneDxLifecycle

            source, resolved, graph = self.generated_boms[plan.component_revision]
            self.process_records.remember_bytes(source)
            self.process_records.remember_bytes(resolved)
            self.process_records.remember_json(graph.to_dict())
            source_bom = validate_cyclonedx_bom(
                source, lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=graph
            )
            resolved_bom = validate_cyclonedx_bom(
                resolved,
                lifecycle=CycloneDxLifecycle.RESOLVED,
                managed_graph=graph,
                source_content=source,
                source_managed_graph=graph,
            )
        else:
            resolved_bom = replace(
                prototype.build.resolved_sbom,
                source_bom_identity=source_bom.bom_identity,
                bom_identity=_identity("resolved-bom-" + name),
            )
        build = replace(
            prototype.build,
            component_revision=plan.component_revision,
            build_plan_identity=plan.identity,
            source_tree_identity=output.candidate.tree_identity,
            source_custody_identity=self.process_records.remember_json(
                {
                    "schema": "literate-ai/local-generated-source-custody@1",
                    "candidate_identity": self.process_records.remember_json(
                        output.candidate.to_dict()
                    ).uri,
                    "source_generation_identity": output.identity.uri,
                    "source_tree_identity": output.candidate.tree_identity.uri,
                    "source_bom_identity": output.candidate.source_bom_identity.uri,
                    "managed_graph_identity": source_bom.managed_graph_identity.uri,
                    "generated_test_suite_identity": (
                        output.candidate.generated_test_suite_identity.uri
                    ),
                }
            ),
            source_sbom=source_bom,
            resolved_sbom=resolved_bom,
            exports=built.exports,
            resolved_sbom_export_identities=tuple(
                item.identity for item in built.exports
            ),
        )
        empty = self.process_records.remember_json("")
        process = self.process_records.remember_json(
            {
                "schema": "literate-ai/local-process-observation@1",
                "phase": "build",
                "plan_identity": plan.identity.uri,
                "returncode": 0,
                "stdout_identity": empty.uri,
                "stderr_identity": empty.uri,
            }
        )
        tree_files = [
            {
                "path": ".literate/resolved-sbom.cdx.json",
                "sha256": resolved_bom.bom_identity.digest,
            },
            {"path": "fixture-output", "sha256": _identity("artifact-" + name).digest},
        ]
        tree = self.process_records.remember_json(
            {"schema": "literate-ai/local-source-tree@1", "files": tree_files}
        )
        observation = self.process_records.remember_json(
            {
                "schema": "literate-ai/local-build-observation@1",
                "build_plan_identity": plan.identity.uri,
                "process_observation_identity": process.uri,
                "artifact_tree_identity": tree.uri,
                "resolved_sbom_identity": build.resolved_sbom.bom_identity.uri,
            }
        )
        manifest = self.process_records.remember_bytes(
            json.dumps(
                {
                    "tree": tree.uri,
                    "build_observation": observation.uri,
                    "provider_materials": [],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        final_tree = self.process_records.remember_json(
            {
                "schema": "literate-ai/local-source-tree@1",
                "files": [
                    tree_files[0],
                    {"path": "artifact-manifest.json", "sha256": manifest.digest},
                    tree_files[1],
                ],
            }
        )
        custody = self.process_records.remember_json(
            {
                "schema": "literate-ai/local-artifact-custody@1",
                "build_plan_identity": plan.identity.uri,
                "artifact_tree_identity": final_tree.uri,
                "export_identities": [item.identity.uri for item in build.exports],
            }
        )
        build = replace(
            build,
            build_observation_identity=observation,
            artifact_custody_identity=custody,
        )
        prototype_case = prototype.generated_tests.cases[0]
        definitions = (
            json.loads(self.generated_suites[plan.component_revision])["cases"]
            if plan.component_revision in self.generated_suites
            else [
                {
                    "case_id": prototype_case.case_id,
                    "category": "normal",
                    "specification_refs": ["component.md"],
                    "arguments": [],
                    "expected_result": name,
                }
            ]
        )
        case_records = sorted(
            (
                (
                    self.process_records.remember_json(
                        {"schema": "literate-ai/generated-test-case@1", **definition}
                    ),
                    definition,
                )
                for definition in definitions
            ),
            key=lambda item: item[0].uri,
        )
        test_stdout = self.process_records.remember_json(
            json.dumps(
                {
                    "schema": "literate-ai/generated-test-results@1",
                    "cases": [
                        {"case_id": definition["case_id"], "outcome": "passed"}
                        for _, definition in case_records
                    ],
                }
            )
        )
        test_process = self.process_records.remember_json(
            {
                "schema": "literate-ai/local-process-observation@1",
                "phase": "generated-test",
                "plan_identity": plan.identity.uri,
                "returncode": 0,
                "stdout_identity": test_stdout.uri,
                "stderr_identity": empty.uri,
            }
        )
        cases = tuple(
            replace(
                prototype_case,
                case_id=definition["case_id"],
                case_identity=case_id,
                observation_identity=self.process_records.remember_json(
                    {
                        "schema": "literate-ai/generated-test-case-observation@1",
                        "observation_kind": "attributed-suite-case",
                        "suite_process_observation_identity": test_process.uri,
                        "case_identity": case_id.uri,
                        "case_result": {
                            "case_id": definition["case_id"],
                            "outcome": "passed",
                        },
                    }
                ),
            )
            for case_id, definition in case_records
        )
        tests = replace(
            prototype.generated_tests,
            component_revision=plan.component_revision,
            generated_test_suite_identity=output.candidate.generated_test_suite_identity,
            build_evidence_identity=build.identity,
            export_identities=build.export_identities,
            selected_case_identities=tuple(case.case_identity for case in cases),
            cases=cases,
            selected_count=len(cases),
            executed_count=len(cases),
            passed_count=len(cases),
        )
        stdout_identity = self.process_records.remember_json(name)
        stderr_identity = self.process_records.remember_json("")
        process_identity = self.process_records.remember_json(
            {
                "schema": "literate-ai/local-process-observation@1",
                "phase": "execute",
                "plan_identity": plan.identity.uri,
                "returncode": 0,
                "stdout_identity": stdout_identity.uri,
                "stderr_identity": stderr_identity.uri,
            }
        )
        execution = replace(
            prototype.execution,
            observation_identity=process_identity,
            stdout_identity=stdout_identity,
            stderr_identity=stderr_identity,
            component_revision=plan.component_revision,
            build_evidence_identity=build.identity,
            provider_artifact_identities=plan.provider_artifact_identities,
            export_identities=build.export_identities,
            root_export_identity=build.exports[0].identity,
            artifact_custody_identity=build.artifact_custody_identity,
        )
        self.typed_acceptances[plan.component_revision.uri] = replace(
            prototype,
            component_revision=plan.component_revision,
            source_generation_identity=output.identity,
            generated_test_suite_identity=tests.generated_test_suite_identity,
            build=build,
            generated_tests=tests,
            execution=execution,
        )
        return StandardBuildOutput(build.exports, build.identity, build)

    def test(self, plan, exports):
        super().test(plan, exports)
        return self.typed_acceptances[plan.component_revision.uri].generated_tests

    def execute(self, plan, exports):
        super().execute(plan, exports)
        return self.typed_acceptances[plan.component_revision.uri].execution

    def accept(self, plan, test_identity, execution_identity):
        super().accept(plan, test_identity, execution_identity)
        return self.typed_acceptances[plan.component_revision.uri]


class _VerboseContextPorts(LifecyclePorts):
    def publish(self, membership):
        if not verbose_enabled():
            raise RuntimeError("verbose context was not propagated")
        return super().publish(membership)


class MismatchedAuthorizationPorts(LifecyclePorts):
    def authorize(self, intent, index):
        authorization = super().authorize(intent, index)
        return replace(authorization, index_identity=_identity("another-index"))


class MismatchedGrantPorts(LifecyclePorts):
    def authorize(self, intent, index):
        authorization = super().authorize(intent, index)
        return replace(
            authorization,
            grant=replace(
                authorization.grant,
                request_digest=_identity("another-build-request").uri,
            ),
        )


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


class MismatchedReceiptPorts(LifecyclePorts):
    def issue(self, receipt):
        super().issue(receipt)
        return _identity("another-receipt")


class _UnsafePhaseError(RuntimeError):
    code = "../../secret token"


class PhaseFailurePorts(LifecyclePorts):
    def __init__(self, execution, names, phase):
        super().__init__(execution, names)
        self.phase = phase

    def _fail(self, phase, name):
        if self.phase == phase and name == "money":
            self._record(phase, name)
            raise _UnsafePhaseError("token=hunter2 at /private/secret/source")

    def __call__(self, prepared):
        name = self.names[prepared.plan.component_revision.uri]
        self._fail("generate", name)
        return super().__call__(prepared)

    def create(
        self,
        execution_plan,
        generation_plan,
        source_candidate,
        provider_artifacts,
        package_artifacts,
    ):
        name = self.names[generation_plan.component_revision.uri]
        self._fail("intent", name)
        return super().create(
            execution_plan,
            generation_plan,
            source_candidate,
            provider_artifacts,
            package_artifacts,
        )

    def index(self, revision, source):
        name = self.names[revision.uri]
        self._fail("index", name)
        return super().index(revision, source)

    def authorize(self, intent, index):
        name = self.names[intent.component_revision.uri]
        self._fail("authorize", name)
        return super().authorize(intent, index)

    def finalize(self, intent, authorization):
        name = self.names[intent.component_revision.uri]
        self._fail("finalize", name)
        return super().finalize(intent, authorization)

    def build(self, plan, provider_artifacts):
        name = self.names[plan.component_revision.uri]
        self._fail("build", name)
        return super().build(plan, provider_artifacts)

    def test(self, plan, exports):
        name = self.names[plan.component_revision.uri]
        self._fail("test", name)
        return super().test(plan, exports)

    def execute(self, plan, exports):
        name = self.names[plan.component_revision.uri]
        self._fail("execute", name)
        return super().execute(plan, exports)

    def accept(self, plan, test_identity, execution_identity):
        name = self.names[plan.component_revision.uri]
        self._fail("accept", name)
        return super().accept(plan, test_identity, execution_identity)

    def publish(self, membership):
        name = self.names[membership.component_revision.uri]
        self._fail("publish", name)
        return super().publish(membership)


class _RepairableBuildError(RuntimeError):
    code = "builder.generated-source-rejected"


class RepairableBuildPorts(LifecyclePorts):
    def __init__(self, execution, names, *, rejected_attempts):
        super().__init__(execution, names)
        self.rejected_attempts = rejected_attempts

    def build(self, plan, provider_artifacts):
        name = self.names[plan.component_revision.uri]
        if name == "money" and self.rejected_attempts > 0:
            self.rejected_attempts -= 1
            self._record("build", name)
            raise _RepairableBuildError("relative/money.cc: declaration rejected")
        return super().build(plan, provider_artifacts)


class _RepairPort:
    def __init__(self):
        self.preparations = []

    def diagnose(self, failure, output):
        return CandidateRepairDiagnostic(
            failure.component_revision,
            failure.phase,
            failure.code,
            "compiler rejected a generated declaration in relative/money.cc",
            CandidateFailureClassification.RETRYABLE,
            failure.identity,
        )

    def prepare_repair(self, original, diagnostic, predecessor_attempt_identities):
        attempt = len(predecessor_attempt_identities)
        prompt = (
            original.request.prompt
            + (
                f"\nrepair-diagnostic:{diagnostic.identity.uri}"
                f"\nrepair-predecessor:{predecessor_attempt_identities[-1].uri}"
            ).encode()
        )
        prompt_identity = ContentIdentity.parse_uri(
            f"sha256:{hashlib.sha256(prompt).hexdigest()}"
        )
        request = replace(original.request.request, prompt_identity=prompt_identity)
        workspace = replace(
            original.workspace,
            allocation_identity=_identity(f"repair-workspace-{attempt}"),
            locator=f"repair-workspace-{attempt}",
        )
        prepared = replace(
            original,
            request=PreparedComponentGenerationRequest(request, prompt),
            workspace=workspace,
        )
        self.preparations.append(prepared)
        return prepared


class PackagedExecutionFailurePorts(LifecyclePorts):
    def execute_packaged_project(
        self, component_lock, execution_plan, project_build_plan, plan, result
    ):
        self._record("execute-package")
        raise RuntimeError("packaged execution failed")


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
    def test_retained_candidate_still_requires_all_acceptance_gates(self):
        class RetainedPorts(LifecyclePorts):
            def __call__(self, prepared):
                output = super().__call__(prepared)
                provenance = replace(
                    output.provenance,
                    route_decision_identities=(),
                    model_stage_output_identities=(),
                    retained_source_identity=_identity("reviewed-retained-input"),
                )
                return replace(
                    output,
                    provenance=provenance,
                    provenance_identity=provenance.identity,
                )

        for fail_acceptance in (False, True):
            with self.subTest(fail_acceptance=fail_acceptance):
                ports = RetainedPorts(self.execution, self.names)
                if fail_acceptance:

                    def reject(*_args):
                        raise RuntimeError(
                            "independent acceptance rejected retained source"
                        )

                    ports.accept_project_independently = reject

                def execute(ports=ports):
                    return _service(ports).execute(
                        self.execution,
                        component_lock=self.lock,
                        invalidation=_decision(
                            self.execution,
                            self.names,
                            "money",
                            tuple(self.names.values()),
                        ),
                        prepared_nodes=self.nodes,
                        max_parallelism=1,
                    )

                if fail_acceptance:
                    with self.assertRaisesRegex(RuntimeError, "independent acceptance"):
                        execute()
                    self.assertEqual(ports.published_memberships, {})
                    self.assertEqual(ports.issued_receipts, [])
                    self.assertNotIn(("admit", "project"), ports.events)
                else:
                    result = execute()
                    self.assertTrue(result.successful)
                    self.assertTrue(ports.issued_receipts)
                    for node in result.node_results:
                        self.assertIs(
                            node.source_generation.disposition,
                            SourceGenerationDisposition.RETAINED,
                        )
                        self.assertIsNotNone(
                            node.source_output.provenance.retained_source_identity
                        )

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

    def test_diamond_uses_provider_artifacts_before_consumer_builds(self):
        self._configure_dependencies(DependencyKind.BUILD)
        ports = LifecyclePorts(self.execution, self.names)
        result = _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution,
                self.names,
                "money",
                ("money", "pricing", "reporting", "invoice-cli"),
            ),
            prepared_nodes=self.nodes,
            max_parallelism=2,
        )
        self.assertTrue(result.successful)
        build_order = [name for stage, name in ports.events if stage == "build"]
        positions = {name: build_order.index(name) for name in build_order}
        self.assertLess(positions["money"], positions["pricing"])
        self.assertLess(positions["money"], positions["reporting"])
        self.assertLess(positions["pricing"], positions["invoice-cli"])
        self.assertLess(positions["reporting"], positions["invoice-cli"])
        event_positions = {event: index for index, event in enumerate(ports.events)}
        self.assertLess(
            event_positions[("accept", "money")],
            event_positions[("intent", "pricing")],
        )

        self.assertLess(
            event_positions[("accept", "money")],
            event_positions[("intent", "reporting")],
        )
        self.assertLess(
            event_positions[("accept", "pricing")],
            event_positions[("intent", "invoice-cli")],
        )
        self.assertLess(
            event_positions[("accept", "reporting")],
            event_positions[("intent", "invoice-cli")],
        )

        self.assertEqual(
            ports.planned_sources,
            {
                name: canonical_identity({"generated": name})
                for name in self.names.values()
            },
        )
        for name in self.names.values():
            for stage in (
                "generate",
                "intent",
                "index",
                "authorize",
                "finalize",
                "build",
                "test",
                "execute",
                "accept",
            ):
                self.assertEqual(ports.events.count((stage, name)), 1)
            self.assertLess(
                event_positions[("generate", name)],
                event_positions[("index", name)],
            )
            self.assertLess(
                event_positions[("index", name)],
                event_positions[("intent", name)],
            )
            self.assertLess(
                event_positions[("intent", name)],
                event_positions[("authorize", name)],
            )
            self.assertLess(
                event_positions[("authorize", name)],
                event_positions[("finalize", name)],
            )
        self.assertEqual(
            ports.events[-2:], [("admit", "project"), ("receipt", "project")]
        )
        self.assertEqual(
            tuple(item.component_revision.uri for item in result.node_results),
            tuple(sorted(item.component_revision.uri for item in result.node_results)),
        )
        self.assertEqual(
            tuple(
                item.component_revision.uri
                for item in result.lifecycle_membership.planned_nodes
            ),
            tuple(item.component_revision.uri for item in result.node_results),
        )
        self.assertEqual(
            result.lifecycle_membership.lifecycle_result_identities,
            tuple(item.identity for item in result.node_results),
        )
        self.assertEqual(len(ports.issued_receipts), 1)
        self.assertEqual(result.aggregate_receipt, ports.issued_receipts[0])
        self.assertIsNotNone(result.root_integration)
        self.assertEqual(
            result.root_integration.identity,
            result.root_integration_evidence_identity,
        )
        self.assertEqual(
            result.root_integration.package_result.package_plan_identity,
            result.root_integration.package_plan.identity,
        )
        self.assertEqual(
            ports.issued_receipts[0].lifecycle_membership_identity,
            result.lifecycle_membership.identity,
        )
        tampered_decision = replace(
            result.lifecycle_membership.cache_decisions[0],
            lifecycle_result_identity=_identity("substituted-lifecycle-result"),
        )
        with self.assertRaisesRegex(
            StandardProjectLifecycleError, "every exact lifecycle result"
        ):
            replace(
                result,
                lifecycle_membership=replace(
                    result.lifecycle_membership,
                    cache_decisions=(
                        tampered_decision,
                        *result.lifecycle_membership.cache_decisions[1:],
                    ),
                ),
            )
        for node_results in (
            result.node_results[:-1],
            (*result.node_results, result.node_results[0]),
        ):
            with self.assertRaisesRegex(
                StandardLifecycleMembershipError, "every and only planned Component"
            ):
                assemble_standard_lifecycle_membership(
                    self.execution,
                    node_results,
                    input_membership_identities={},
                    forced_regeneration=frozenset(),
                )
        with self.assertRaisesRegex(
            StandardProjectLifecycleError, "exact project membership"
        ):
            replace(
                result,
                aggregate_receipt=replace(
                    result.aggregate_receipt,
                    lifecycle_membership_identity=_identity("substituted-membership"),
                ),
            )
        with self.assertRaisesRegex(
            StandardProjectLifecycleError, "retained root integration"
        ):
            replace(
                result,
                root_integration_evidence_identity=_identity(
                    "substituted-root-integration"
                ),
            )
        forged_membership = _identity("forged-accepted-membership")
        forged_decision = replace(
            result.lifecycle_membership.cache_decisions[0],
            accepted_membership_identity=forged_membership,
            publication_identity=forged_membership,
        )
        with self.assertRaisesRegex(
            StandardProjectLifecycleError, "lifecycle result evidence"
        ):
            replace(
                result,
                lifecycle_membership=replace(
                    result.lifecycle_membership,
                    cache_decisions=(
                        forged_decision,
                        *result.lifecycle_membership.cache_decisions[1:],
                    ),
                ),
                aggregate_receipt=replace(
                    result.aggregate_receipt,
                    lifecycle_membership_identity=replace(
                        result.lifecycle_membership,
                        cache_decisions=(
                            forged_decision,
                            *result.lifecycle_membership.cache_decisions[1:],
                        ),
                    ).identity,
                ),
            )

    def test_toolchain_providers_bind_exact_accepted_artifacts_before_build(self):
        self._configure_dependencies(DependencyKind.TOOLCHAIN)
        ports = LifecyclePorts(self.execution, self.names)
        result = _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution, self.names, "money", tuple(self.names.values())
            ),
            prepared_nodes=self.nodes,
            max_parallelism=2,
        )
        self.assertTrue(result.successful)
        positions = {event: index for index, event in enumerate(ports.events)}
        edges = {
            edge.identity.uri: edge
            for action in self.execution.action_plans
            for edge in action.dependency_edges
        }.values()
        for generation in self.execution.generation_plans:
            uri = generation.component_revision.uri
            incoming = tuple(
                edge for edge in edges if edge.consumer_revision.uri == uri
            )
            expected = {
                artifact.identity
                for edge in incoming
                if edge.kind is DependencyKind.TOOLCHAIN
                for artifact in ports.realized_exports[edge.provider_revision.uri]
            }
            plan = ports.plans[uri]
            self.assertEqual(set(plan.provider_artifact_identities), expected)
            self.assertEqual(
                {
                    item.identity
                    for item in plan.manifest.actions[0].dependency_artifacts
                },
                expected,
            )
            for artifact in ports.realized_exports[uri]:
                self.assertEqual(set(artifact.dependency_artifact_identities), expected)
            for edge in incoming:
                if edge.kind is DependencyKind.TOOLCHAIN:
                    self.assertLess(
                        positions[("accept", self.names[edge.provider_revision.uri])],
                        positions[("intent", self.names[uri])],
                    )

    def test_assembly_requires_locked_late_edges_and_exact_provider_acceptance(self):
        from literate_ai.application.release_artifacts import (
            ReleaseArtifactAssemblyError,
            create_standard_artifact_build_graph,
            plan_standard_assembly_dependencies,
        )

        for kind in (
            DependencyKind.RUNTIME,
            DependencyKind.PACKAGING,
        ):
            with self.subTest(kind=kind):
                self._configure_dependencies(kind)
                ports = LifecyclePorts(self.execution, self.names)
                arguments = dict(
                    component_lock=self.lock,
                    invalidation=_decision(
                        self.execution, self.names, "money", tuple(self.names.values())
                    ),
                    prepared_nodes=self.nodes,
                    max_parallelism=2,
                )
                result = _service(ports).execute(self.execution, **arguments)
                self.assertTrue(result.successful)
                self.assertEqual(
                    create_standard_artifact_build_graph(
                        self.execution, result
                    ).identity,
                    result.root_integration.artifact_graph.identity,
                )
                dependencies = plan_standard_assembly_dependencies(
                    self.execution, result.node_results
                )
                self.assertTrue(dependencies)
                package = result.root_integration.package_plan
                self.assertTrue(
                    {item.provider_artifact_identity for item in dependencies}
                    <= {item.source_identity for item in package.inputs}
                )
                self.assertEqual(len(package.entrypoints), 1)
                for invalid_results in (
                    result.node_results[:-1],
                    (*result.node_results, result.node_results[0]),
                ):
                    with self.assertRaises(ReleaseArtifactAssemblyError):
                        plan_standard_assembly_dependencies(
                            self.execution, invalid_results
                        )
                results = {
                    item.component_revision: item for item in result.node_results
                }
                exports = {
                    item.identity: node
                    for node in result.node_results
                    for item in node.exports
                }
                locked_edges = {
                    edge.identity: edge
                    for action in self.execution.action_plans
                    for edge in action.dependency_edges
                }
                for item in dependencies:
                    edge = locked_edges[item.dependency_edge_identity]
                    self.assertEqual(item.dependency_kind, kind)
                    self.assertEqual(
                        exports[item.consumer_artifact_identity].component_revision,
                        edge.consumer_revision,
                    )
                    self.assertEqual(
                        exports[item.provider_artifact_identity].component_revision,
                        edge.provider_revision,
                    )
                    self.assertEqual(
                        item.provider_acceptance_identity,
                        results[edge.provider_revision].acceptance_identity,
                    )

                class MissingAssemblyPorts(LifecyclePorts):
                    def assemble_project_artifacts(self, *args):
                        graph, link = super().assemble_project_artifacts(*args)
                        # Rebuild a valid but incomplete closure; checking graph
                        # structure alone cannot detect missing locked authority.
                        missing = create_artifact_build_graph(
                            build_system_driver_identity=graph.build_system_driver_identity,
                            manifests=graph.manifests,
                            link_roots=(link.root_artifact_identity,),
                        )
                        return missing, missing.link_plans[0]

                with self.assertRaisesRegex(
                    StandardProjectLifecycleError, "locked late dependencies"
                ):
                    _service(MissingAssemblyPorts(self.execution, self.names)).execute(
                        self.execution, **arguments
                    )

    def test_package_only_provider_does_not_block_local_consumer_build(self):
        lock = _diamond_lock(include_invoice_money_packaging_edge=True)
        execution, requests = _prepared_execution(lock)
        nodes = _prepared_nodes(execution, requests)
        names = _names(lock)
        provider_started = threading.Event()
        consumer_built = threading.Event()

        class PackageOverlapPorts(LifecyclePorts):
            overlapped = False

            def build(self, plan, provider_artifacts):
                name = self.names[plan.component_revision.uri]
                if name == "money":
                    provider_started.set()
                    self.overlapped = consumer_built.wait(5)
                elif name == "invoice-cli":
                    if not provider_started.wait(5):
                        raise AssertionError("package provider did not start")
                output = super().build(plan, provider_artifacts)
                if name == "invoice-cli":
                    consumer_built.set()
                return output

        ports = PackageOverlapPorts(execution, names)
        result = _service(ports).execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=nodes,
            max_parallelism=2,
        )
        self.assertTrue(result.successful)
        self.assertTrue(ports.overlapped, "consumer waited for packaging-only provider")
        self.assertLess(
            ports.events.index(("build", "invoice-cli")),
            ports.events.index(("accept", "money")),
        )
        money = next(
            item
            for item in result.node_results
            if names[item.component_revision.uri] == "money"
        )
        self.assertIn(
            money.exports[0].identity,
            {
                item.source_identity
                for item in result.root_integration.package_plan.inputs
            },
        )

    def test_changed_package_provider_changes_package_not_consumer_build_identity(self):
        lock = _diamond_lock(include_invoice_money_packaging_edge=True)
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        arguments = dict(
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=_prepared_nodes(execution, requests),
            max_parallelism=2,
        )
        original = LifecyclePorts(execution, names)
        changed = LifecyclePorts(
            execution,
            names,
            changed_exports={"money"},
            export_label="changed-package-provider",
        )
        first = _service(original).execute(execution, **arguments)
        second = _service(changed).execute(execution, **arguments)
        self.assertTrue(first.successful)
        self.assertTrue(second.successful)
        root = execution.root_revision.uri
        self.assertEqual(original.plans[root].identity, changed.plans[root].identity)
        self.assertEqual(
            original.realized_exports[root], changed.realized_exports[root]
        )
        self.assertNotEqual(
            first.root_integration.package_plan.identity,
            second.root_integration.package_plan.identity,
        )
        self.assertNotEqual(
            first.root_integration.artifact_graph.identity,
            second.root_integration.artifact_graph.identity,
        )
        money = next(uri for uri, name in names.items() if name == "money")
        package_inputs = {
            item.source_identity for item in second.root_integration.package_plan.inputs
        }
        self.assertIn(changed.realized_exports[money][0].identity, package_inputs)
        self.assertNotIn(original.realized_exports[money][0].identity, package_inputs)

    def test_failed_package_provider_preserves_compilation_but_blocks_publication(self):
        lock = _diamond_lock(include_invoice_money_packaging_edge=True)
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        ports = LifecyclePorts(execution, names, fail_build=("money",))
        result = _service(ports).execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=_prepared_nodes(execution, requests),
            max_parallelism=2,
        )
        self.assertFalse(result.successful)
        self.assertIn(("build", "invoice-cli"), ports.events)
        self.assertNotIn(("create-package", "project"), ports.events)
        self.assertNotIn(("receipt", "project"), ports.events)
        self.assertFalse(ports.published_memberships)

    def test_packaging_provider_is_separate_from_build_but_joins_package_authority(
        self,
    ):
        """#110: package provenance is retained without a process binding.

        ``invoice-cli`` here sees pricing/reporting only through generation-visible
        public interfaces and has a packaging-kind edge straight to money. Public
        interfaces are already admitted by the lock. Money joins only the final
        assembly closure, so its output does not alter compilation provenance or
        require a build-time process environment binding.
        """

        lock = _diamond_lock(include_invoice_money_packaging_edge=True)
        execution, requests = _prepared_execution(lock)
        nodes = _prepared_nodes(execution, requests)
        names = _names(lock)
        ports = LifecyclePorts(execution, names)

        result = _service(ports).execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=nodes,
            max_parallelism=2,
        )

        self.assertTrue(result.successful)
        invoice_uri = next(uri for uri, name in names.items() if name == "invoice-cli")
        money_uri = next(uri for uri, name in names.items() if name == "money")

        invoice_provider_identities = set(
            ports.plans[invoice_uri].provider_artifact_identities
        )
        expected: set[ContentIdentity] = set()
        self.assertEqual(invoice_provider_identities, expected)
        self.assertNotIn(
            ports.realized_exports[money_uri][0].identity, invoice_provider_identities
        )
        self.assertEqual(
            ports.plans[invoice_uri].package_artifact_identities,
            (),
        )

        self.assertEqual(
            tuple(
                item.identity
                for item in ports.plans[invoice_uri]
                .manifest.actions[0]
                .dependency_artifacts
            ),
            tuple(sorted(expected, key=lambda item: item.uri)),
        )
        self.assertEqual(
            tuple(
                item.identity
                for item in ports.plans[invoice_uri]
                .manifest.actions[0]
                .package_dependency_artifacts
            ),
            (),
        )
        self.assertEqual(
            set(ports.realized_exports[invoice_uri][0].dependency_artifact_identities),
            set(),
        )
        graph = result.root_integration.artifact_graph
        self.assertEqual(
            {item.provider_artifact_identity for item in graph.assembly_dependencies},
            {ports.realized_exports[money_uri][0].identity},
        )
        self.assertIn(
            ports.realized_exports[money_uri][0].identity,
            {
                item.source_identity
                for item in result.root_integration.package_plan.inputs
            },
        )

    def test_public_interface_provider_does_not_bind_build_artifact(self):
        lock = _diamond_lock()
        public_edges = tuple(
            edge for edge in lock.edges if edge.public_interface_identity is not None
        )
        lock = replace(lock, edges=public_edges)
        execution, requests = _prepared_execution(lock)
        nodes = _prepared_nodes(execution, requests)
        names = _names(lock)
        ports = LifecyclePorts(execution, names)

        result = _service(ports).execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=nodes,
            max_parallelism=2,
        )

        self.assertTrue(result.successful)
        self.assertTrue(ports.plans)
        self.assertTrue(
            all(not plan.provider_artifact_identities for plan in ports.plans.values())
        )

    def test_ready_consumer_runs_while_unrelated_previous_layer_is_busy(self):
        lock = _diamond_lock(independent_reporting=True)
        names = _names(lock)
        execution, requests = _prepared_execution(lock)
        nodes = _prepared_nodes(execution, requests)
        consumer_started = threading.Event()
        reporting_started = threading.Event()

        class OverlapPorts(LifecyclePorts):
            overlapped = False

            def build(self, plan, provider_artifacts):
                name = names[plan.component_revision.uri]
                if name == "reporting":
                    reporting_started.set()
                    self.overlapped = consumer_started.wait(5)
                elif name == "money":
                    if not reporting_started.wait(5):
                        raise AssertionError("independent worker did not start")
                elif name == "pricing":
                    consumer_started.set()
                return super().build(plan, provider_artifacts)

        ports = OverlapPorts(execution, names)
        result = _service(ports).execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=nodes,
            max_parallelism=2,
        )
        self.assertTrue(result.successful)
        self.assertTrue(ports.overlapped, "consumer waited for an unrelated layer peer")

    def test_locked_interface_consumer_build_overlaps_provider_build(self):
        provider_started = threading.Event()
        consumer_built = threading.Event()
        names = self.names

        class InterfacePorts(LifecyclePorts):
            overlapped = False

            def build(self, plan, provider_artifacts):
                name = names[plan.component_revision.uri]
                if name == "money":
                    provider_started.set()
                    self.overlapped = consumer_built.wait(5)
                elif name == "pricing":
                    if not provider_started.wait(5):
                        raise AssertionError("provider build did not start")
                result = super().build(plan, provider_artifacts)
                if name == "pricing":
                    consumer_built.set()
                return result

        ports = InterfacePorts(self.execution, names)
        result = _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution, names, "money", tuple(names.values())
            ),
            prepared_nodes=self.nodes,
            max_parallelism=2,
        )
        self.assertTrue(result.successful)
        self.assertTrue(ports.overlapped, "locked interface waited for provider build")
        self.assertLess(
            ports.events.index(("build", "pricing")),
            ports.events.index(("accept", "money")),
        )

    def test_failed_provider_does_not_cancel_locked_interface_consumers(self):
        ports = LifecyclePorts(self.execution, self.names, fail_build={"money"})
        result = _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution, self.names, "money", tuple(self.names.values())
            ),
            prepared_nodes=self.nodes,
            max_parallelism=2,
        )
        by_name = {
            self.names[item.component_revision.uri]: item
            for item in result.node_results
        }
        self.assertFalse(result.successful)
        self.assertIsNotNone(by_name["money"].failure_code)
        for name in ("pricing", "reporting", "invoice-cli"):
            self.assertIsNone(by_name[name].failure_code)
            self.assertIn(("build", name), ports.events)
        self.assertNotIn(("admit", "project"), ports.events)
        self.assertNotIn(("receipt", "project"), ports.events)

    def test_consumer_generation_overlaps_provider_build_but_import_waits(self):
        consumer_generated = threading.Event()
        provider_build_started = threading.Event()
        provider_accepted = threading.Event()
        slot_lock = threading.Lock()
        occupancy = {"active": 0, "peak": 0}

        @contextmanager
        def occupy_slot():
            with slot_lock:
                occupancy["active"] += 1
                occupancy["peak"] = max(occupancy.values())
            try:
                yield
            finally:
                with slot_lock:
                    occupancy["active"] -= 1

        lock = _diamond_lock(dependency_kind=DependencyKind.BUILD)
        execution, requests = _prepared_execution(lock)
        nodes = _prepared_nodes(execution, requests)
        names = _names(lock)

        class OverlapPorts(LifecyclePorts):
            overlapped = False

            def __call__(self, prepared):
                with occupy_slot():
                    name = names[prepared.plan.component_revision.uri]
                    if name == "pricing" and not provider_build_started.wait(5):
                        raise AssertionError(
                            "provider build never overlapped generation"
                        )
                    output = super().__call__(prepared)
                    if name == "pricing":
                        consumer_generated.set()
                    return output

            def build(self, plan, provider_artifacts):
                with occupy_slot():
                    name = names[plan.component_revision.uri]
                    if name == "money":
                        provider_build_started.set()
                        self.overlapped = consumer_generated.wait(5)
                    elif name == "pricing":
                        if not provider_accepted.is_set():
                            raise AssertionError("consumer used an unaccepted provider")
                    return super().build(plan, provider_artifacts)

            def accept(self, plan, test_identity, execution_identity):
                result = super().accept(plan, test_identity, execution_identity)
                if names[plan.component_revision.uri] == "money":
                    provider_accepted.set()
                return result

        ports = OverlapPorts(execution, names)
        result = _service(ports).execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=nodes,
            max_parallelism=2,
        )
        self.assertTrue(result.successful)
        self.assertTrue(
            ports.overlapped, "consumer generation waited for provider build"
        )
        self.assertEqual(occupancy, {"active": 0, "peak": 2})
        self.assertEqual(
            [name for stage, name in ports.events if stage == "generate"].count(
                "pricing"
            ),
            1,
        )

    def test_consumer_index_overlaps_provider_build_but_intent_waits(self):
        consumer_indexed = threading.Event()
        provider_started = threading.Event()
        provider_accepted = threading.Event()
        lock = _diamond_lock(dependency_kind=DependencyKind.BUILD)
        execution, requests = _prepared_execution(lock)
        nodes = _prepared_nodes(execution, requests)
        names = _names(lock)

        class OverlapPorts(LifecyclePorts):
            overlapped = False

            def index(self, revision, source):
                if names[revision.uri] == "pricing":
                    if not provider_started.wait(5):
                        raise AssertionError("provider build did not start")
                    consumer_indexed.set()
                return super().index(revision, source)

            def create(self, execution_plan, generation_plan, *args):
                if names[generation_plan.component_revision.uri] == "pricing":
                    if not provider_accepted.is_set():
                        raise AssertionError("intent used an unaccepted provider")
                return super().create(execution_plan, generation_plan, *args)

            def build(self, plan, provider_artifacts):
                if names[plan.component_revision.uri] == "money":
                    provider_started.set()
                    self.overlapped = consumer_indexed.wait(5)
                return super().build(plan, provider_artifacts)

            def accept(self, plan, test_identity, execution_identity):
                result = super().accept(plan, test_identity, execution_identity)
                if names[plan.component_revision.uri] == "money":
                    provider_accepted.set()
                return result

        ports = OverlapPorts(execution, names)
        result = _service(ports).execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=nodes,
            max_parallelism=2,
        )
        self.assertTrue(result.successful)
        self.assertTrue(ports.overlapped, "consumer indexing waited for provider build")

    def test_component_build_phases_are_dispatched_as_separate_pool_operations(self):
        ports = LifecyclePorts(self.execution, self.names)
        service = _service(ports)
        original = service._run_step
        stages = []
        guard = threading.Lock()

        def dispatch(step):
            self.assertIsNot(threading.current_thread(), threading.main_thread())
            with guard:
                stages.append(step.stage)
            return original(step)

        service._run_step = dispatch
        result = service.execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution, self.names, "money", tuple(self.names.values())
            ),
            prepared_nodes=self.nodes,
            max_parallelism=2,
        )
        self.assertTrue(result.successful)
        self.assertEqual(
            Counter(stages),
            {
                stage: len(self.nodes)
                for stage in (
                    StandardLifecycleStage.SOURCE_GENERATION,
                    StandardLifecycleStage.BUILD_INTENT,
                    StandardLifecycleStage.SOURCE_INDEX,
                    StandardLifecycleStage.BUILD_AUTHORIZATION,
                    StandardLifecycleStage.BUILD_PLAN,
                    StandardLifecycleStage.BUILD,
                    StandardLifecycleStage.TEST,
                    StandardLifecycleStage.EXECUTE,
                    StandardLifecycleStage.ACCEPT,
                )
            },
        )

    def test_malformed_dispatched_build_response_cannot_unlock_later_phases(self):
        ports = LifecyclePorts(self.execution, self.names)
        service = _service(ports)
        original = service._run_step
        stages = []

        def dispatch(step):
            stages.append(step.stage)
            if step.stage is StandardLifecycleStage.BUILD:
                return object()
            return original(step)

        service._run_step = dispatch
        result = service.execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution, self.names, "money", tuple(self.names.values())
            ),
            prepared_nodes=self.nodes,
            max_parallelism=2,
        )
        self.assertFalse(result.successful)
        money = next(
            item
            for item in result.node_results
            if self.names[item.component_revision.uri] == "money"
        )
        self.assertEqual(money.failure_code, "standard_lifecycle.build_output_invalid")
        for stage in (
            StandardLifecycleStage.TEST,
            StandardLifecycleStage.EXECUTE,
            StandardLifecycleStage.ACCEPT,
        ):
            self.assertNotIn(stage, stages)
        self.assertNotIn(("admit", "project"), ports.events)
        self.assertNotIn(("receipt", "project"), ports.events)

    def test_queued_host_phase_rechecks_authorization_when_it_starts(self):
        for selected in (
            StandardLifecycleStage.BUILD,
            StandardLifecycleStage.TEST,
            StandardLifecycleStage.EXECUTE,
            StandardLifecycleStage.ACCEPT,
        ):
            with self.subTest(phase=selected):
                ports = LifecyclePorts(self.execution, self.names)
                service = _service(ports)
                original = service._run_step
                now = [datetime(2026, 8, 7, 0, 1, tzinfo=UTC)]
                service.clock = lambda now=now: now[0]

                def dispatch(step, selected=selected, now=now, original=original):
                    if step.stage is selected:
                        now[0] += timedelta(hours=1)
                    return original(step)

                service._run_step = dispatch
                result = service.execute(
                    self.execution,
                    component_lock=self.lock,
                    invalidation=_decision(
                        self.execution, self.names, "money", tuple(self.names.values())
                    ),
                    prepared_nodes=self.nodes,
                    max_parallelism=2,
                )
                self.assertFalse(result.successful)
                expired = [
                    item
                    for item in result.node_results
                    if item.failure_code == "security.authorization_expired"
                    and item.failure_evidence.phase
                    is StandardNodeFailurePhase(selected.value)
                ]
                self.assertTrue(
                    expired, "queued phase did not reject expired authority"
                )
                for item in expired:
                    name = self.names[item.component_revision.uri]
                    self.assertNotIn((selected.value, name), ports.events)
                self.assertNotIn(("admit", "project"), ports.events)
                self.assertNotIn(("receipt", "project"), ports.events)

    def test_run_scoped_diagnostics_propagate_to_parallel_component_nodes(self):
        ports = _VerboseContextPorts(self.execution, self.names)
        with verbose_diagnostics(True):
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

    def test_root_integration_ports_run_in_order_before_admission_and_receipt(self):
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

        project_stages = [
            stage for stage, component in ports.events if component == "project"
        ]
        self.assertEqual(
            project_stages,
            [
                "validate",
                "assemble-artifacts",
                "create-package",
                "root-integration-test",
                "execute-package",
                "independent-accept",
                "admit",
                "receipt",
            ],
        )
        self.assertEqual(
            result.aggregate_receipt.context_prompt_journal_identities,
            result.context_prompt_journal_identities,
        )
        self.assertEqual(
            result.aggregate_receipt.context_benchmark_record_identities,
            tuple(item.identity for item in result.context_benchmark_records),
        )
        self.assertEqual(
            result.aggregate_receipt.context_cache_report_identity,
            result.context_cache_report.identity,
        )
        self.assertIsNotNone(result.root_integration_evidence_identity)
        assert result.aggregate_receipt is not None
        self.assertEqual(
            result.aggregate_receipt.root_integration_evidence_identity,
            result.root_integration_evidence_identity,
        )
        last_component_accept = max(
            index
            for index, (stage, component) in enumerate(ports.events)
            if stage == "accept" and component != "project"
        )
        self.assertLess(
            last_component_accept,
            ports.events.index(("assemble-artifacts", "project")),
        )
        independent_accept = ports.events.index(("independent-accept", "project"))
        first_publication = min(
            index
            for index, (stage, _component) in enumerate(ports.events)
            if stage == "publish"
        )
        self.assertLess(independent_accept, first_publication)
        self.assertLess(
            max(
                index
                for index, (stage, _component) in enumerate(ports.events)
                if stage == "publish"
            ),
            ports.events.index(("admit", "project")),
        )

        recorder = QualificationEvidenceRecorder(max_bytes=5_000_000, max_records=100)
        capture_lifecycle_records(result, recorder)
        records = dict(recorder.entries)
        self.assertEqual(
            json.loads(records[result.identity])["root_integration_evidence_identity"],
            result.root_integration.identity.uri,
        )
        root = result.root_integration
        self.assertEqual(type(root).from_dict(json.loads(records[root.identity])), root)
        reader = QualificationEvidenceReader(
            recorder.entries, max_bytes=5_000_000, max_records=100
        )
        reopened = reopen_qualification_lifecycle(reader, result.identity)
        self.assertEqual(reopened, result)
        self.assertEqual(reopened.identity, result.identity)
        self.assertEqual(reopened.project_build_plan, result.project_build_plan)
        self.assertEqual(reopened.root_integration, result.root_integration)
        for missing in (
            result.node_results[0].identity,
            result.node_results[0].source_cache_membership.identity,
            result.project_build_plan.components[0].identity,
            result.generation_schedule.identity,
            result.context_benchmark_records[0].identity,
            result.context_cache_report.identity,
        ):
            incomplete = QualificationEvidenceReader(
                tuple(item for item in recorder.entries if item[0] != missing),
                max_bytes=5_000_000,
                max_records=100,
            )
            with (
                self.subTest(missing=missing),
                self.assertRaisesRegex(QualificationCaptureError, "record-missing"),
            ):
                reopen_qualification_lifecycle(incomplete, result.identity)
        for changes in (
            {"admission_identity": _identity("foreign-admission").uri},
            {"schema": "unreviewed-lifecycle"},
            {"generation_schedule_identity": None},
        ):
            forged = recorder.remember_json({**result.identity_document(), **changes})
            modified = QualificationEvidenceReader(
                recorder.entries, max_bytes=5_000_000, max_records=100
            )
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(QualificationCaptureError, "lifecycle-invalid"),
            ):
                reopen_qualification_lifecycle(modified, forged)
        duplicate_node = result.node_results[0].identity
        forged = recorder.remember_json(
            {
                **result.identity_document(),
                "node_results": [duplicate_node.uri] * 2000,
            }
        )
        modified = QualificationEvidenceReader(
            recorder.entries, max_bytes=5_000_000, max_records=100
        )
        with patch.object(modified, "read_json", wraps=modified.read_json) as read:
            with self.assertRaisesRegex(QualificationCaptureError, "lifecycle-invalid"):
                reopen_qualification_lifecycle(modified, forged)
            self.assertEqual(
                sum(call.args == (duplicate_node,) for call in read.call_args_list), 1
            )
        for record in (
            result.generation_schedule,
            result.context_cache_report,
            *result.context_benchmark_records,
            *(node.source_cache_membership for node in result.node_results),
        ):
            self.assertEqual(
                canonical_identity(json.loads(records[record.identity])),
                record.identity,
            )
        self.assertEqual(
            json.loads(records[result.project_build_plan.identity])["components"],
            [item.identity.uri for item in result.project_build_plan.components],
        )
        for node in result.node_results:
            document = json.loads(records[node.identity])
            self.assertEqual(
                document["acceptance_identity"], node.acceptance_identity.uri
            )
        for identity, payload in records.items():
            self.assertEqual(canonical_identity(json.loads(payload)), identity)
        rejected = QualificationEvidenceRecorder(max_bytes=5_000_000, max_records=100)
        with patch.object(
            type(result),
            "identity",
            new_callable=PropertyMock,
            return_value=_identity("substituted-lifecycle"),
        ):
            with self.assertRaisesRegex(QualificationCaptureError, "record-mismatch"):
                capture_lifecycle_records(result, rejected)
        self.assertEqual(rejected.entries, ())

    def test_reopened_root_requires_the_exact_lifecycle_and_project_receipt(self):
        from literate_ai.application.standard_test_receipts import _aggregate
        from literate_ai.contracts.testing import ProjectTestEvidence
        from tests.support.fixtures_test_qualification_capture import fixture
        from tests.support.fixtures_test_qualification_lifecycle_runner import _receipt

        ports = LifecyclePorts(self.execution, self.names)
        lifecycle = _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution, self.names, "money", tuple(self.names.values())
            ),
            prepared_nodes=self.nodes,
            max_parallelism=2,
        )
        # These service ports return stage identities, not native typed build
        # evidence. Use the contract receipt fixture to test root/receipt binding;
        # this fixture cannot establish complete producer qualification.
        receipt = _receipt(lifecycle)
        run = fixture()[0]
        request, invocation = _identity("request"), _identity("invocation")

        def ordered(items):
            return tuple(sorted(items, key=lambda item: item.uri))

        run = replace(
            run,
            component_lock_identity=self.execution.component_lock_identity,
            target_profile_identity=lifecycle.root_integration.package_plan.target_identity,
            lifecycle_result_identity=lifecycle.identity,
            lifecycle_policy_identity=receipt.suite.content_identity,
            lifecycle_request_identity=request,
            lifecycle_invocation_identity=invocation,
            generated_test_total=receipt.summary.total,
            acceptance_evidence_identities=ordered(
                n.acceptance_identity for n in lifecycle.node_results
            ),
            build_evidence_identities=ordered(
                n.build_identity for n in lifecycle.node_results
            ),
            source_index_identities=ordered(
                n.index_identity for n in lifecycle.node_results
            ),
            generated_test_evidence_identities=ordered(
                n.test_identity for n in lifecycle.node_results
            ),
        )
        evidence = {item.kind: item.identity for item in receipt.evidence}
        evidence.update(
            {
                "lifecycle-command": invocation,
                "lifecycle-request": request,
                "lifecycle-plan": lifecycle.execution_plan_identity,
                "source-cache-lifecycle": lifecycle.lifecycle_membership.identity,
                "workspace-admission": lifecycle.admission_identity,
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
                ProjectTestEvidence(kind, value)
                for kind, value in sorted(evidence.items())
            ),
        )
        run = replace(run, project_receipt_identity=receipt.identity)
        recorder = QualificationEvidenceRecorder(max_bytes=5_000_000, max_records=100)
        capture_lifecycle_records(lifecycle, recorder)
        recorder.remember_json(receipt.to_dict())
        reader = QualificationEvidenceReader(
            recorder.entries, max_bytes=5_000_000, max_records=100
        )
        # Reopening uses only immutable records after producer references go away.
        expected = lifecycle.root_integration
        del ports, lifecycle
        self.assertEqual(reopen_qualification_root(reader, run), expected)
        with self.assertRaisesRegex(QualificationCaptureError, "run-incomplete"):
            reopen_qualification_run(reader, run)
        for field in (
            "component_lock_identity",
            "target_profile_identity",
            "lifecycle_policy_identity",
            "lifecycle_request_identity",
            "lifecycle_invocation_identity",
        ):
            with (
                self.subTest(field=field),
                self.assertRaises(QualificationCaptureError),
            ):
                reopen_qualification_root(
                    reader, replace(run, **{field: _identity("foreign")})
                )
        for field in (
            "acceptance_evidence_identities",
            "build_evidence_identities",
            "source_index_identities",
            "resolved_sbom_identities",
            "generated_test_evidence_identities",
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(QualificationCaptureError, "receipt-mismatch"),
            ):
                reopen_qualification_root(
                    reader, replace(run, **{field: (_identity("foreign"),)})
                )
        for missing in (
            run.lifecycle_result_identity,
            receipt.identity,
            expected.identity,
            receipt.subject_identity,
        ):
            incomplete = QualificationEvidenceReader(
                tuple(item for item in recorder.entries if item[0] != missing),
                max_bytes=5_000_000,
                max_records=100,
            )
            with (
                self.subTest(missing=missing),
                self.assertRaisesRegex(QualificationCaptureError, "record-missing"),
            ):
                reopen_qualification_root(incomplete, run)

        # A rehashed receipt pointing at another result must not cross the binding.
        for substituted in (
            replace(receipt, result_identity=_identity("foreign-result")),
            replace(receipt, subject_identity=_identity("foreign-subject")),
            replace(receipt, evidence=receipt.evidence[:-1]),
        ):
            recorder.remember_json(substituted.to_dict())
            reader = QualificationEvidenceReader(
                recorder.entries, max_bytes=5_000_000, max_records=100
            )
            with self.assertRaises(QualificationCaptureError):
                reopen_qualification_root(
                    reader, replace(run, project_receipt_identity=substituted.identity)
                )

    def captured_qualification_run_fixture(
        self,
        *,
        wrong_source_bom=False,
        library_exports=False,
        root_acceptance=None,
        recorder=None,
        generated_suites=None,
        generated_boms=None,
        artifact_target_identity=None,
        artifact_payloads=None,
    ):
        from literate_ai.application.standard_test_receipts import (
            project_standard_project_test_receipt,
        )
        from literate_ai.contracts.standard_lifecycle_policy import (
            load_current_standard_lifecycle_policy,
        )
        from literate_ai.contracts.testing import ProjectTestReceiptPolicy
        from literate_ai.source_to_specification.qualification_lifecycle import (
            QualificationLifecycleExecution,
            QualificationLifecycleRunner,
        )
        from tests.support.fixtures_test_qualification_capture import fixture

        ports = ContractEvidenceLifecyclePorts(
            self.execution,
            self.names,
            wrong_source_bom=wrong_source_bom,
            library_exports=library_exports,
            root_acceptance=root_acceptance,
            generated_suites=generated_suites,
            generated_boms=generated_boms,
        )
        from tests.support.fixtures_test_application_generation import execution_plan

        ports.generation_execution_plan = execution_plan(self)
        ports.artifact_payloads = artifact_payloads or {}
        if artifact_target_identity is not None:
            ports.artifact_target_identity = artifact_target_identity
        lifecycle = _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution, self.names, "money", tuple(self.names.values())
            ),
            prepared_nodes=self.nodes,
            max_parallelism=2,
        )
        self.assertTrue(lifecycle.successful)
        policy = load_current_standard_lifecycle_policy()
        run = fixture()[0]
        from literate_ai.contracts.projects import StandardProjectLifecycleDriver

        driver = StandardProjectLifecycleDriver(
            run.framework_distribution_identity, policy.identity
        )
        run = replace(run, driver_identity=driver.identity)
        receipt = project_standard_project_test_receipt(
            lifecycle,
            project_id="typed-qualification-fixture",
            project_revision_identity=_identity("project"),
            lifecycle_policy=policy,
            receipt_policy=ProjectTestReceiptPolicy(
                policy.policy_id,
                policy.policy_version,
                run.driver_identity,
                policy.required_evidence_kinds,
                1,
            ),
            lifecycle_request_identity=run.lifecycle_request_identity,
            lifecycle_invocation_identity=run.lifecycle_invocation_identity,
            runner_identity=run.driver_identity,
        )
        execution = QualificationLifecycleExecution(
            lifecycle,
            receipt,
            run.lifecycle_request_identity,
            run.lifecycle_invocation_identity,
            run.driver_identity,
            policy.identity,
            run.framework_distribution_identity,
        )
        derived = QualificationLifecycleRunner._derive_standard(execution)
        names = (
            "source_tree_identities",
            "source_index_identities",
            "build_evidence_identities",
            "resolved_sbom_identities",
            "generated_test_suite_identities",
            "generated_test_evidence_identities",
            "generated_test_case_identities",
            "acceptance_evidence_identities",
            "cache_decision_identities",
            "node_workspace_identities",
        )
        run = replace(
            run,
            **dict(zip(names, derived, strict=True)),
            lifecycle_result_identity=lifecycle.identity,
            project_receipt_identity=receipt.identity,
            lifecycle_policy_identity=policy.identity,
            component_lock_identity=self.execution.component_lock_identity,
            target_profile_identity=lifecycle.root_integration.package_plan.target_identity,
            generated_test_total=receipt.summary.total,
        )
        recorder = recorder or QualificationEvidenceRecorder(
            max_bytes=5_000_000, max_records=1000
        )
        for _, payload in ports.process_records.entries:
            recorder.remember_bytes(payload)
        capture_lifecycle_records(lifecycle, recorder)
        recorder.remember_json(receipt.to_dict())
        reader = QualificationEvidenceReader(
            recorder.entries, max_bytes=5_000_000, max_records=1000
        )
        return reader, run, execution

    def test_reopened_run_derives_complete_typed_membership(self):
        reader, run, execution = self.captured_qualification_run_fixture()
        self.assertEqual(reopen_qualification_run(reader, run), execution)
        for name in (
            "source_tree_identities",
            "generated_test_suite_identities",
            "generated_test_case_identities",
            "cache_decision_identities",
            "node_workspace_identities",
        ):
            with (
                self.subTest(field=name),
                self.assertRaisesRegex(QualificationCaptureError, "run-mismatch"),
            ):
                reopen_qualification_run(
                    reader, replace(run, **{name: (_identity("foreign"),)})
                )
        for name in (
            "source_index_identities",
            "build_evidence_identities",
            "resolved_sbom_identities",
            "generated_test_evidence_identities",
            "acceptance_evidence_identities",
        ):
            with (
                self.subTest(field=name),
                self.assertRaisesRegex(QualificationCaptureError, "receipt-mismatch"),
            ):
                reopen_qualification_run(
                    reader, replace(run, **{name: (_identity("foreign"),)})
                )

    def test_internally_bound_stages_cannot_substitute_the_generated_source_bom(self):
        reader, run, _ = self.captured_qualification_run_fixture(wrong_source_bom=True)
        with self.assertRaisesRegex(QualificationCaptureError, "source-chain-mismatch"):
            reopen_qualification_run(reader, run)

    def test_verifier_admitted_fanout_never_invokes_generator(self):
        accepted = self._accepted_baseline()
        admissions = {}
        for uri, candidate in accepted.items():
            generation = candidate.generation
            source = generation.output.candidate
            plan = self.nodes[uri].plan
            evidence = StandardSourceAdmissionEvidence(
                component_revision_identity=source.component_revision,
                component_lock_identity=self.execution.component_lock_identity,
                generation_plan_identity=plan.identity,
                generation_key_identity=plan.generation_key.identity,
                recipe_identity=source.recipe_identity,
                orchestration_request_identity=(
                    source.source_generation_request_identity
                ),
                planned_coding_cli_request_identity=(
                    source.planned_coding_cli_request_identity
                ),
                flavor_set_identity=plan.generation_key.flavor_selection_identity,
                skill_closure_identity=_identity(f"skills-{uri}"),
                source_tree_identity=source.tree_identity,
                source_bundle_identity=source.source_bundle_identity,
                source_manifest_identity=source.source_manifest_identity,
                source_bom_identity=source.source_bom_identity,
                generated_test_suite_identity=source.generated_test_suite_identity,
                coding_cli_tool_binding_identity=_identity("coding-cli"),
                coding_cli_transcript_identity=_identity(f"transcript-{uri}"),
                generation_provenance_identity=generation.output.provenance_identity,
                test_plan_identity=_identity(f"source-test-plan-{uri}"),
                test_results=(
                    StandardSourceTestResult(
                        _identity(f"source-oracle-{uri}"),
                        _identity(f"source-result-{uri}"),
                        3,
                        3,
                        0,
                        0,
                    ),
                ),
                source_selectors=StandardSourceSelectorSet(
                    StandardSourceSelectorScope.TARGET_INDEPENDENT, ()
                ),
                framework_distribution_identity=_identity("framework"),
                verifier_identity=_identity("source-verifier"),
                admitted_at="2026-08-14T00:00:00Z",
            )
            admissions[uri] = StandardSourceAdmissionMembership(generation, evidence)

        ports = LifecyclePorts(self.execution, self.names)
        result = _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(self.execution, self.names, "money"),
            prepared_nodes=self.nodes,
            source_cache_memberships=admissions,
            max_parallelism=2,
        )

        self.assertTrue(result.successful)
        self.assertFalse(any(stage == "generate" for stage, _name in ports.events))
        self.assertEqual(
            {item.source_admission_identity for item in result.node_results},
            {membership.identity for membership in admissions.values()},
        )

    def test_lock_authority_mismatch_fails_before_validation_or_generation(self):
        ports = LifecyclePorts(self.execution, self.names)
        foreign_lock = _single_component_lock(self.lock, "money")
        with self.assertRaisesRegex(StandardProjectLifecycleError, "exact authority"):
            _service(ports).execute(
                self.execution,
                component_lock=foreign_lock,
                invalidation=_decision(self.execution, self.names, "money"),
                prepared_nodes=self.nodes,
            )
        self.assertEqual(ports.events, [])
        self.assertEqual(ports.issued_receipts, [])

    def test_root_mismatch_fails_before_validation_or_generation(self):
        ports = LifecyclePorts(self.execution, self.names)
        non_root = next(
            item.component_revision
            for item in self.execution.generation_plans
            if item.component_revision != self.lock.root_revision
        )
        mismatched = replace(self.execution, root_revision=non_root)
        with self.assertRaisesRegex(StandardProjectLifecycleError, "plan root"):
            _service(ports).execute(
                mismatched,
                component_lock=self.lock,
                invalidation=_decision(self.execution, self.names, "money"),
                prepared_nodes=self.nodes,
            )
        self.assertEqual(ports.events, [])
        self.assertEqual(ports.issued_receipts, [])

    def test_root_port_failure_prevents_acceptance_admission_and_receipt(self):
        ports = PackagedExecutionFailurePorts(self.execution, self.names)
        recorder = _CheckpointRecorder()
        with self.assertRaisesRegex(RuntimeError, "packaged execution failed"):
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
        project_stages = [
            stage for stage, component in ports.events if component == "project"
        ]
        self.assertEqual(
            project_stages,
            [
                "validate",
                "assemble-artifacts",
                "create-package",
                "root-integration-test",
                "execute-package",
            ],
        )
        self.assertEqual(ports.published_memberships, {})
        self.assertFalse(any(stage == "publish" for stage, _ in ports.events))
        self.assertFalse(
            any(
                item.stage is StandardLifecycleStage.SOURCE_CACHE_PUBLICATION
                for item in recorder.evidence
            )
        )
        self.assertEqual(ports.issued_receipts, [])

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

    def test_complete_prepared_nodes_are_mandatory_before_generation(self):
        ports = LifecyclePorts(self.execution, self.names)
        incomplete = dict(self.nodes)
        incomplete.pop(next(iter(incomplete)))

        with self.assertRaisesRegex(
            StandardProjectLifecycleError, "every and only executable Component"
        ):
            _service(ports).execute(
                self.execution,
                component_lock=self.lock,
                invalidation=_decision(self.execution, self.names, "money"),
                prepared_nodes=incomplete,
            )

        self.assertEqual(ports.events, [])

    def test_intent_rejects_build_request_for_another_source(self):
        revision = next(iter(self.names))
        source = _identity("source")
        with self.assertRaisesRegex(
            StandardProjectLifecycleError, "exact Component and source bundle"
        ):
            StandardComponentBuildIntent(
                canonical_identity({"revision": revision}),
                source,
                _identity("source-bundle"),
                BuildRequest(
                    effective_revision_digest=canonical_identity(
                        {"revision": revision}
                    ).uri,
                    source_bundle_digest=_identity("another-source").uri,
                    builder_id="builder:fixture@1",
                    toolchain_digest=_identity("toolchain").uri,
                    sandbox_profile="fixture",
                    requested_privileges=("execute-build-tools",),
                    allowed_outputs=("artifact",),
                ),
            )

    def test_authorization_binding_mismatch_fails_before_finalization(self):
        ports = MismatchedAuthorizationPorts(self.execution, self.names)
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

    def test_grant_for_another_request_fails_before_finalization(self):
        ports = MismatchedGrantPorts(self.execution, self.names)
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

    def test_coding_cli_diagnostic_survives_standard_source_failure(self):
        ports = LifecyclePorts(self.execution, self.names)
        service = _service(ports)

        def generate(prepared):
            name = self.names[prepared.plan.component_revision.uri]
            if name == "money":
                raise CodingCliError(
                    "coding_cli.generated_metadata_invalid",
                    "invalid generated metadata secret=topsecret "
                    "at /Users/operator/private/candidate.json",
                )
            return ports(prepared)

        service.generator = generate
        result = service.execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution,
                self.names,
                "money",
                ("money",),
            ),
            prepared_nodes=self.nodes,
            max_parallelism=1,
        )
        by_name = {
            self.names[item.component_revision.uri]: item
            for item in result.node_results
        }
        failed = by_name["money"]
        self.assertEqual(
            failed.source_generation.failure_code,
            "coding_cli.generated_metadata_invalid",
        )
        self.assertEqual(
            failed.failure_evidence.code,
            "coding_cli.generated_metadata_invalid",
        )
        self.assertEqual(
            failed.failure_evidence.phase,
            StandardNodeFailurePhase.SOURCE_GENERATION,
        )
        self.assertIn("<redacted>", failed.failure_evidence.diagnostic)
        self.assertNotIn("topsecret", failed.failure_evidence.diagnostic)
        self.assertNotIn("/Users/operator", failed.failure_evidence.diagnostic)

    def test_rejected_provider_preserves_index_without_consumer_intent_or_repair(self):
        lock = _diamond_lock(
            dependency_kind=DependencyKind.BUILD, independent_reporting=True
        )
        execution, requests = _prepared_execution(lock)
        nodes = _prepared_nodes(execution, requests)
        names = _names(lock)
        ports = PhaseFailurePorts(execution, names, "accept")
        diagnosed = []

        class TerminalRepairPort(_RepairPort):
            def diagnose(self, failure, output):
                diagnosed.append(names[failure.component_revision.uri])
                return CandidateRepairDiagnostic(
                    failure.component_revision,
                    failure.phase,
                    failure.code,
                    "independent acceptance rejected the provider",
                    CandidateFailureClassification.TERMINAL,
                    failure.identity,
                )

        repair = TerminalRepairPort()
        recorder = _CheckpointRecorder()
        result = _service(
            ports, candidate_repair_port=repair, checkpoint_recorder=recorder
        ).execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=nodes,
            max_parallelism=2,
        )
        by_name = {
            names[node.component_revision.uri]: node for node in result.node_results
        }
        self.assertFalse(result.successful)
        self.assertIsNone(by_name["reporting"].failure_evidence)
        self.assertIs(
            by_name["money"].failure_evidence.phase, StandardNodeFailurePhase.ACCEPT
        )
        for name in ("pricing", "invoice-cli"):
            node = by_name[name]
            self.assertEqual(node.failure_code, "dependency.failed")
            self.assertIs(
                node.failure_evidence.phase, StandardNodeFailurePhase.DEPENDENCY
            )
            self.assertIsNotNone(node.index_identity)
            self.assertIn(("index", name), ports.events)
            for stage in ("intent", "authorize", "build", "accept", "publish"):
                self.assertNotIn((stage, name), ports.events)
            self.assertTrue(
                any(
                    item.component_revision == node.component_revision
                    and item.stage is StandardLifecycleStage.SOURCE_INDEX
                    and item.outcome is StandardLifecycleCheckpointOutcome.PASSED
                    for item in recorder.evidence
                )
            )
        self.assertEqual(diagnosed, ["money"])
        self.assertEqual(repair.preparations, [])
        self.assertNotIn(("admit", "project"), ports.events)
        self.assertNotIn(("receipt", "project"), ports.events)

    def test_execution_refuses_missing_or_substituted_provider_evidence(self):
        self._configure_dependencies(DependencyKind.BUILD)
        for changed in ((), (_identity("substituted-provider"),)):
            with self.subTest(changed=changed):

                class ChangedExecutionPorts(ContractEvidenceLifecyclePorts):
                    def execute(self, plan, exports):
                        evidence = super().execute(plan, exports)
                        if self.names[plan.component_revision.uri] == "pricing":
                            return replace(
                                evidence,
                                provider_artifact_identities=self.changed_inputs,
                            )
                        return evidence

                ports = ChangedExecutionPorts(self.execution, self.names)
                ports.changed_inputs = changed
                result = _service(ports).execute(
                    self.execution,
                    component_lock=self.lock,
                    invalidation=_decision(
                        self.execution, self.names, "money", tuple(self.names.values())
                    ),
                    prepared_nodes=self.nodes,
                    max_parallelism=2,
                )
                self.assertFalse(result.successful)
                rejected = next(
                    item
                    for item in result.node_results
                    if self.names[item.component_revision.uri] == "pricing"
                )
                self.assertEqual(
                    rejected.failure_evidence.code,
                    "standard_lifecycle.execution_evidence_mismatch",
                )
                self.assertNotIn(("accept", "pricing"), ports.events)
                self.assertNotIn(("receipt", "project"), ports.events)

    def test_each_phase_has_sanitized_typed_failure_and_stops_downstream(self):
        cases = (
            (
                "generate",
                StandardNodeFailurePhase.SOURCE_GENERATION,
                "runner-failed",
                "intent",
            ),
            (
                "intent",
                StandardNodeFailurePhase.BUILD_INTENT,
                "build-intent.failed",
                "authorize",
            ),
            (
                "index",
                StandardNodeFailurePhase.SOURCE_INDEX,
                "source-index.failed",
                "intent",
            ),
            (
                "authorize",
                StandardNodeFailurePhase.BUILD_AUTHORIZATION,
                "build-authorization.failed",
                "finalize",
            ),
            (
                "finalize",
                StandardNodeFailurePhase.BUILD_PLAN,
                "build-plan.failed",
                "build",
            ),
            ("build", StandardNodeFailurePhase.BUILD, "build.failed", "test"),
            ("test", StandardNodeFailurePhase.TEST, "test.failed", "execute"),
            ("execute", StandardNodeFailurePhase.EXECUTE, "execute.failed", "accept"),
            ("accept", StandardNodeFailurePhase.ACCEPT, "accept.failed", "publish"),
            (
                "publish",
                StandardNodeFailurePhase.SOURCE_CACHE_PUBLICATION,
                "source-cache-publication.failed",
                "admit",
            ),
        )
        money_uri = next(uri for uri, name in self.names.items() if name == "money")
        for port_phase, evidence_phase, code, downstream in cases:
            with self.subTest(phase=port_phase):
                ports = PhaseFailurePorts(self.execution, self.names, port_phase)
                recorder = _CheckpointRecorder()
                result = _service(ports, checkpoint_recorder=recorder).execute(
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
                node = next(
                    item
                    for item in result.node_results
                    if item.component_revision.uri == money_uri
                )
                evidence = node.failure_evidence
                self.assertIsNotNone(evidence)
                self.assertEqual(evidence.phase, evidence_phase)
                self.assertEqual(evidence.code, code)
                self.assertEqual(node.failure_code, code)
                self.assertNotIn((downstream, "money"), ports.events)
                self.assertIsNone(node.acceptance_identity)
                self.assertIsNone(node.source_cache_membership)
                node_checkpoints = [
                    item
                    for item in recorder.evidence
                    if item.component_revision.uri == money_uri
                ]
                if evidence_phase is StandardNodeFailurePhase.SOURCE_GENERATION:
                    self.assertEqual(node_checkpoints, [])
                else:
                    self.assertEqual(
                        node_checkpoints[-1].stage.value, evidence_phase.value
                    )
                    self.assertEqual(
                        node_checkpoints[-1].outcome,
                        StandardLifecycleCheckpointOutcome.FAILED,
                    )
                    self.assertEqual(node_checkpoints[-1].failure_code, code)
                self.assertIsNone(node.source_cache_publication_identity)
                decision = next(
                    item
                    for item in result.lifecycle_membership.cache_decisions
                    if item.component_revision.uri == money_uri
                )
                self.assertEqual(decision.failure_identity, evidence.identity)
                serialized = repr(evidence.to_dict())
                self.assertNotIn("hunter2", serialized)
                self.assertNotIn("/private/secret", serialized)
                self.assertFalse(result.successful)
                self.assertNotIn(("admit", "project"), ports.events)
                self.assertNotIn(("receipt", "project"), ports.events)

    def test_stale_resume_build_identity_rebuilds_only_that_node(self):
        resume = self._accepted_baseline()
        ports = LifecyclePorts(self.execution, self.names)
        reporting_uri = next(
            uri for uri, name in self.names.items() if name == "reporting"
        )
        resume[reporting_uri] = replace(
            resume[reporting_uri], build_plan_identity=_identity("stale-plan")
        )
        result = _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(self.execution, self.names, "money"),
            prepared_nodes=self.nodes,
            resume_candidates=resume,
            max_parallelism=2,
        )
        generated = [name for stage, name in ports.events if stage == "generate"]
        built = [name for stage, name in ports.events if stage == "build"]
        self.assertEqual(generated, [])
        self.assertEqual(built, ["reporting"])
        self.assertTrue(result.successful)

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

    def test_source_cache_hit_is_reindexed_reauthorized_and_not_republished(self):
        accepted = self._accepted_baseline()
        money_uri = next(uri for uri, name in self.names.items() if name == "money")
        ports = LifecyclePorts(self.execution, self.names)

        result = _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(self.execution, self.names, "money"),
            prepared_nodes=self.nodes,
            source_cache_memberships={
                money_uri: accepted[money_uri].source_cache_membership
            },
            max_parallelism=2,
        )

        self.assertTrue(result.successful)
        self.assertNotIn(("generate", "money"), ports.events)
        for stage in ("index", "authorize", "build", "test", "execute", "accept"):
            self.assertIn((stage, "money"), ports.events)
        self.assertNotIn(("publish", "money"), ports.events)
        self.assertEqual(
            sorted(name for stage, name in ports.events if stage == "publish"),
            sorted(name for name in self.names.values() if name != "money"),
        )
        decisions = {
            self.names[item.component_revision.uri]: item
            for item in result.lifecycle_membership.cache_decisions
        }
        self.assertIs(decisions["money"].outcome, StandardNodeCacheOutcome.HIT)
        self.assertIsNone(decisions["money"].publication_identity)
        for name, decision in decisions.items():
            if name != "money":
                self.assertIs(decision.outcome, StandardNodeCacheOutcome.MISS)
                self.assertEqual(
                    decision.publication_identity,
                    decision.accepted_membership_identity,
                )

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

    def test_explicit_regeneration_bypasses_and_replaces_cache_membership(self):
        accepted = self._accepted_baseline()
        money_uri = next(uri for uri, name in self.names.items() if name == "money")
        ports = LifecyclePorts(self.execution, self.names)

        result = _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution,
                self.names,
                "money",
                ("money",),
            ),
            prepared_nodes=self.nodes,
            source_cache_memberships={
                money_uri: accepted[money_uri].source_cache_membership
            },
            max_parallelism=2,
        )

        self.assertTrue(result.successful)
        self.assertIn(("generate", "money"), ports.events)
        self.assertIn(("publish", "money"), ports.events)
        money_decision = next(
            item
            for item in result.lifecycle_membership.cache_decisions
            if self.names[item.component_revision.uri] == "money"
        )
        self.assertIs(
            money_decision.outcome,
            StandardNodeCacheOutcome.FORCED_REGENERATION,
        )

    def test_membership_for_another_workspace_reuses_source_under_current_custody(
        self,
    ):
        accepted = self._accepted_baseline()
        money_uri = next(uri for uri, name in self.names.items() if name == "money")
        nodes = dict(self.nodes)
        nodes[money_uri] = replace(
            nodes[money_uri],
            workspace=replace(
                nodes[money_uri].workspace,
                allocation_identity=_identity("fresh-money-workspace"),
                locator="fixture://fresh-money-workspace",
            ),
        )
        ports = LifecyclePorts(self.execution, self.names)

        result = _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(self.execution, self.names, "money"),
            prepared_nodes=nodes,
            source_cache_memberships={
                money_uri: accepted[money_uri].source_cache_membership
            },
            max_parallelism=2,
        )

        self.assertTrue(result.successful)
        self.assertNotIn(("generate", "money"), ports.events)
        self.assertNotIn(("publish", "money"), ports.events)
        money = next(
            item
            for item in result.node_results
            if item.component_revision.uri == money_uri
        )
        self.assertIs(money.disposition, SourceGenerationDisposition.REUSED)
        self.assertEqual(
            money.source_output.candidate.workspace_allocation_identity,
            nodes[money_uri].workspace.allocation_identity,
        )
        self.assertNotEqual(
            money.source_output.identity,
            accepted[money_uri].generation.output.identity,
        )

    def test_equal_generation_key_reuses_source_across_application_roots(self):
        origin_nodes = _root_bound_prepared_nodes(self.execution, self.requests)
        origin_ports = LifecyclePorts(self.execution, self.names)
        origin = _service(origin_ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution,
                self.names,
                "money",
                tuple(self.names.values()),
            ),
            prepared_nodes=origin_nodes,
            max_parallelism=2,
        )
        self.assertTrue(origin.successful)
        money_uri = next(uri for uri, name in self.names.items() if name == "money")
        origin_money = next(
            item
            for item in origin.node_results
            if item.component_revision.uri == money_uri
        )
        origin_membership = origin_money.source_cache_membership

        target_lock = _single_component_lock(self.lock, "money")
        target_execution, target_requests = _prepared_execution(target_lock)
        target_nodes = _root_bound_prepared_nodes(target_execution, target_requests)
        target_names = _names(target_lock)
        target_plan = target_execution.generation_plans[0]
        target_prepared = target_nodes[money_uri]

        self.assertEqual(
            target_plan.component_revision,
            origin_membership.component_revision,
        )
        self.assertEqual(
            target_plan.generation_key.identity,
            origin_membership.generation_key_identity,
        )
        self.assertNotEqual(
            target_execution.root_revision, self.execution.root_revision
        )
        self.assertNotEqual(
            target_execution.component_lock_identity,
            self.execution.component_lock_identity,
        )
        self.assertNotEqual(
            target_plan.identity,
            origin_money.source_output.candidate.component_generation_plan_identity,
        )
        self.assertNotEqual(
            target_prepared.request.request.identity,
            origin_money.source_output.candidate.source_generation_request_identity,
        )
        self.assertNotEqual(
            target_prepared.recipe.identity,
            origin_money.source_output.candidate.recipe_identity,
        )
        self.assertNotEqual(
            target_prepared.workspace.allocation_identity,
            origin_money.source_output.candidate.workspace_allocation_identity,
        )

        target_ports = LifecyclePorts(target_execution, target_names)
        result = _service(target_ports).execute(
            target_execution,
            component_lock=target_lock,
            invalidation=_decision(target_execution, target_names, "money"),
            prepared_nodes=target_nodes,
            source_cache_memberships={money_uri: origin_membership},
        )

        self.assertTrue(result.successful)
        self.assertFalse(
            any(stage == "generate" for stage, _name in target_ports.events)
        )
        for stage in ("index", "authorize", "build", "test", "execute", "accept"):
            self.assertIn((stage, "money"), target_ports.events)
        self.assertNotIn(("publish", "money"), target_ports.events)
        target_money = result.node_results[0]
        self.assertIs(target_money.disposition, SourceGenerationDisposition.REUSED)
        rebound = target_money.source_output
        cached = origin_membership.generation.output
        self.assertEqual(
            rebound.candidate.tree_identity, cached.candidate.tree_identity
        )
        self.assertEqual(
            rebound.candidate.source_bundle_identity,
            cached.candidate.source_bundle_identity,
        )
        self.assertEqual(
            rebound.candidate.source_manifest_identity,
            cached.candidate.source_manifest_identity,
        )
        self.assertEqual(
            rebound.candidate.source_bom_identity,
            cached.candidate.source_bom_identity,
        )
        self.assertEqual(
            rebound.candidate.generated_test_suite_identity,
            cached.candidate.generated_test_suite_identity,
        )
        self.assertEqual(
            rebound.provenance.route_decision_identities,
            cached.provenance.route_decision_identities,
        )
        self.assertEqual(
            rebound.provenance.model_stage_output_identities,
            cached.provenance.model_stage_output_identities,
        )
        self.assertEqual(
            rebound.candidate.source_generation_request_identity,
            target_prepared.request.request.identity,
        )
        self.assertEqual(
            rebound.candidate.component_generation_plan_identity,
            target_plan.identity,
        )
        self.assertEqual(
            rebound.candidate.recipe_identity, target_prepared.recipe.identity
        )
        self.assertEqual(
            rebound.candidate.workspace_allocation_identity,
            target_prepared.workspace.allocation_identity,
        )
        self.assertEqual(
            rebound.provenance.component_lock_identity,
            target_execution.component_lock_identity,
        )
        self.assertEqual(
            rebound.provenance.application_root_revision_identity,
            target_execution.root_revision,
        )
        self.assertNotEqual(rebound.identity, cached.identity)
        decision = result.lifecycle_membership.cache_decisions[0]
        self.assertIs(decision.outcome, StandardNodeCacheOutcome.HIT)
        self.assertEqual(decision.input_membership_identity, origin_membership.identity)
        self.assertEqual(
            decision.accepted_membership_identity,
            target_money.source_cache_membership.identity,
        )
        self.assertIsNone(decision.publication_identity)

    def test_cross_root_reuse_does_not_charge_historical_generation_runtime(self):
        historical_observation = ComponentGenerationRuntimeObservation(
            model_attempts=3,
            wall_time_ms=600_000,
            model_tokens=100_000,
            cost_microunits=50_000_000,
        )
        origin_execution, origin_requests = _prepared_execution_with_budget(
            self.lock,
            _context_budget(),
        )
        origin_nodes = _root_bound_prepared_nodes(origin_execution, origin_requests)
        origin_names = _names(self.lock)
        origin_ports = LifecyclePorts(
            origin_execution,
            origin_names,
            runtime_observations={"money": historical_observation},
        )
        origin = _service(origin_ports).execute(
            origin_execution,
            component_lock=self.lock,
            invalidation=_decision(
                origin_execution,
                origin_names,
                "money",
                tuple(origin_names.values()),
            ),
            prepared_nodes=origin_nodes,
            max_parallelism=2,
        )
        self.assertTrue(origin.successful)
        money_uri = next(uri for uri, name in origin_names.items() if name == "money")
        origin_money = next(
            item
            for item in origin.node_results
            if item.component_revision.uri == money_uri
        )
        membership = origin_money.source_cache_membership
        self.assertEqual(
            membership.generation.output.runtime_observation,
            historical_observation,
        )

        target_lock = _single_component_lock(self.lock, "money")
        target_execution, target_requests = _prepared_execution_with_budget(
            target_lock,
            _context_budget(
                max_model_attempts=1,
                max_wall_time_ms=1,
                max_model_tokens=1,
                max_cost_microunits=1,
            ),
        )
        target_nodes = _root_bound_prepared_nodes(target_execution, target_requests)
        target_names = _names(target_lock)
        self.assertEqual(
            target_execution.generation_plans[0].generation_key.identity,
            membership.generation_key_identity,
        )
        target_ports = LifecyclePorts(target_execution, target_names)

        result = _service(target_ports).execute(
            target_execution,
            component_lock=target_lock,
            invalidation=_decision(target_execution, target_names, "money"),
            prepared_nodes=target_nodes,
            source_cache_memberships={money_uri: membership},
        )

        self.assertTrue(result.successful)
        self.assertFalse(
            any(stage == "generate" for stage, _name in target_ports.events)
        )
        target_money = result.node_results[0]
        self.assertIs(target_money.disposition, SourceGenerationDisposition.REUSED)
        self.assertIsNone(target_money.source_generation.runtime_observation)
        self.assertIsNone(target_money.source_output.runtime_observation)
        self.assertEqual(
            membership.generation.output.runtime_observation,
            historical_observation,
        )

    def test_cache_rebind_rejects_recipe_lock_outside_current_execution(self):
        accepted = self._accepted_baseline()
        money_uri = next(uri for uri, name in self.names.items() if name == "money")
        nodes = dict(self.nodes)
        nodes[money_uri] = replace(
            nodes[money_uri],
            recipe=replace(
                nodes[money_uri].recipe,
                component_lock_identity=_identity("stale-component-lock"),
            ),
        )
        ports = LifecyclePorts(self.execution, self.names)

        result = _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(self.execution, self.names, "money"),
            prepared_nodes=nodes,
            source_cache_memberships={
                money_uri: accepted[money_uri].source_cache_membership
            },
        )

        self.assertFalse(result.successful)
        self.assertNotIn(("generate", "money"), ports.events)
        self.assertNotIn(("build", "money"), ports.events)
        self.assertIn(("build", "pricing"), ports.events)
        self.assertNotIn(("admit", "project"), ports.events)
        self.assertNotIn(("receipt", "project"), ports.events)
        money = next(
            item
            for item in result.node_results
            if item.component_revision.uri == money_uri
        )
        self.assertEqual(
            money.failure_code,
            "standard_lifecycle.cache_custody_lock_mismatch",
        )
        self.assertIs(
            money.failure_evidence.phase, StandardNodeFailurePhase.SOURCE_GENERATION
        )

    def test_generation_key_mismatch_is_rejected_before_cache_reuse(self):
        accepted = self._accepted_baseline()
        money_uri = next(uri for uri, name in self.names.items() if name == "money")
        prior = accepted[money_uri].source_cache_membership
        generation_key = _identity("another-generation-key")
        candidate = replace(
            prior.generation.output.candidate,
            generation_key_identity=generation_key,
        )
        provenance = replace(
            prior.generation.output.provenance,
            generation_key_identity=generation_key,
            candidate_identity=candidate.identity,
        )
        output = replace(
            prior.generation.output,
            candidate=candidate,
            candidate_identity=candidate.identity,
            provenance=provenance,
            provenance_identity=provenance.identity,
        )
        generation = replace(
            prior.generation,
            output=output,
            output_identity=output.identity,
        )
        membership = StandardSourceCacheMembership(
            prior.component_revision,
            generation_key_identity=_identity("another-generation-key"),
            generation=generation,
            acceptance_identity=prior.acceptance_identity,
        )
        self.assertIsInstance(membership.identity, ContentIdentity)
        ports = LifecyclePorts(self.execution, self.names)

        with self.assertRaisesRegex(
            StandardProjectLifecycleError,
            "source-cache membership does not bind its exact planned Component",
        ):
            _service(ports).execute(
                self.execution,
                component_lock=self.lock,
                invalidation=_decision(self.execution, self.names, "money"),
                prepared_nodes=self.nodes,
                source_cache_memberships={money_uri: membership},
            )

        self.assertEqual(ports.events, [])

    def test_receipt_identity_outside_aggregate_membership_fails_closed(self):
        ports = MismatchedReceiptPorts(self.execution, self.names)

        with self.assertRaisesRegex(
            StandardProjectLifecycleError, "outside aggregate membership"
        ):
            _service(ports).execute(
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

    def test_success_without_context_evidence_recorder_fails_closed(self):
        ports = LifecyclePorts(self.execution, self.names)
        with self.assertRaisesRegex(
            StandardProjectLifecycleError, "requires durable context evidence"
        ):
            _service(ports, context_evidence_recorder=None).execute(
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

    def test_retryable_candidate_gets_at_most_two_fresh_replacements(self):
        ports = RepairableBuildPorts(self.execution, self.names, rejected_attempts=2)
        repair = _RepairPort()
        result = _service(ports, candidate_repair_port=repair).execute(
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

        money_revision = next(
            revision for revision, name in self.names.items() if name == "money"
        )
        chain = next(
            item
            for item in result.candidate_attempt_chains
            if item.component_revision.uri == money_revision
        )
        self.assertTrue(result.successful)
        self.assertIs(chain.disposition, CandidateAttemptChainDisposition.ACCEPTED)
        self.assertEqual(len(chain.attempts), 3)
        self.assertEqual(
            tuple(item.attempt_index for item in chain.attempts), (0, 1, 2)
        )
        self.assertEqual(
            chain.attempts[2].predecessor_attempt_identities,
            tuple(item.identity for item in chain.attempts[:2]),
        )
        self.assertEqual(
            len({item.workspace_allocation_identity for item in chain.attempts}), 3
        )
        self.assertEqual(ports.events.count(("index", "money")), 3)
        self.assertEqual(
            sum(event == ("publish", "money") for event in ports.events),
            1,
        )

    def test_repair_exhaustion_never_admits_or_caches_rejected_candidates(self):
        ports = RepairableBuildPorts(self.execution, self.names, rejected_attempts=3)
        result = _service(ports, candidate_repair_port=_RepairPort()).execute(
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

        exhausted = next(
            item
            for item in result.candidate_attempt_chains
            if item.disposition is CandidateAttemptChainDisposition.EXHAUSTED
        )
        self.assertEqual(len(exhausted.attempts), 3)
        self.assertFalse(result.successful)
        self.assertNotIn(("publish", "money"), ports.events)
        self.assertNotIn(("admit", "project"), ports.events)

    def test_changed_authorization_rebuilds_without_regeneration(self):
        resume = self._accepted_baseline()
        prior_plans = {
            uri: candidate.build_plan_identity for uri, candidate in resume.items()
        }
        ports = LifecyclePorts(
            self.execution,
            self.names,
            authorization_label="rotated-authorization",
        )

        result = _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(self.execution, self.names, "money"),
            prepared_nodes=self.nodes,
            resume_candidates=resume,
            max_parallelism=2,
        )

        self.assertTrue(result.successful)
        self.assertEqual(
            [name for stage, name in ports.events if stage == "generate"], []
        )
        self.assertEqual(
            sorted(name for stage, name in ports.events if stage == "build"),
            sorted(self.names.values()),
        )
        for node in result.node_results:
            self.assertNotEqual(
                node.build_plan_identity,
                prior_plans[node.component_revision.uri],
            )
            self.assertEqual(
                node.authorization_identity,
                ports.plans[node.component_revision.uri].request.authorization_identity,
            )

    def test_changed_public_interface_provider_export_does_not_rebuild_consumers(self):
        resume = self._accepted_baseline()
        ports = LifecyclePorts(
            self.execution,
            self.names,
            changed_exports={"money"},
            export_label="rotated",
        )

        result = _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(self.execution, self.names, "money"),
            prepared_nodes=self.nodes,
            resume_candidates=resume,
            max_parallelism=2,
        )

        self.assertTrue(result.successful)
        self.assertFalse(any(stage == "generate" for stage, _ in ports.events))
        self.assertEqual(
            sorted(name for stage, name in ports.events if stage == "build"),
            ["money"],
        )

    def test_checkpoint_recorder_captures_every_successful_stage_and_failure(self):
        recorder = _CheckpointRecorder()
        ports = PhaseFailurePorts(self.execution, self.names, "test")

        result = _service(ports, checkpoint_recorder=recorder).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution,
                self.names,
                "money",
                tuple(self.names.values()),
            ),
            prepared_nodes=self.nodes,
            max_parallelism=1,
        )

        money_revision = next(
            uri for uri, name in self.names.items() if name == "money"
        )
        evidence = [
            item
            for item in recorder.evidence
            if item.component_revision.uri == money_revision
        ]
        self.assertEqual(
            [item.stage for item in evidence],
            [
                StandardLifecycleStage.SOURCE_GENERATION,
                StandardLifecycleStage.SOURCE_INDEX,
                StandardLifecycleStage.BUILD_INTENT,
                StandardLifecycleStage.BUILD_AUTHORIZATION,
                StandardLifecycleStage.BUILD_PLAN,
                StandardLifecycleStage.BUILD,
                StandardLifecycleStage.TEST,
            ],
        )
        self.assertEqual(
            [item.outcome for item in evidence],
            [StandardLifecycleCheckpointOutcome.PASSED] * 6
            + [StandardLifecycleCheckpointOutcome.FAILED],
        )
        self.assertEqual(evidence[-1].failure_code, "test.failed")
        self.assertFalse(result.successful)

    def test_source_checkpoint_resume_replays_gates_without_generation(self):
        single_lock = _single_component_lock(self.lock, "money")
        execution, requests = _prepared_execution(single_lock)
        nodes = _prepared_nodes(execution, requests)
        names = _names(single_lock)
        first_ports = LifecyclePorts(execution, names)
        first = _service(first_ports).execute(
            execution,
            component_lock=single_lock,
            invalidation=_decision(execution, names, "money", ("money",)),
            prepared_nodes=nodes,
        )
        self.assertTrue(first.successful)
        prior = first.node_results[0]
        assert prior.source_output is not None
        resume = SourceGenerationResumeCandidate(
            prior.source_output,
            prior.source_output.identity,
            prior.source_generation.complexity_budget_identity,
            prior.source_generation.complexity_decision_identity,
        )
        second_ports = LifecyclePorts(execution, names)

        second = _service(second_ports).execute(
            execution,
            component_lock=single_lock,
            invalidation=_decision(execution, names, "money"),
            prepared_nodes=nodes,
            source_generation_resume_candidates={prior.component_revision.uri: resume},
        )

        self.assertTrue(second.successful)
        self.assertEqual(
            [event for event in second_ports.events if event[0] == "generate"], []
        )
        self.assertTrue(
            all(
                any(event[0] == stage for event in second_ports.events)
                for stage in (
                    "index",
                    "authorize",
                    "build",
                    "test",
                    "execute",
                    "accept",
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
