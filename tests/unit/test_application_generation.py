"""Application-layer lifecycle ordering, provenance, and restart tests."""

from __future__ import annotations

import hashlib
import json
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from literate_ai.application import (
    ComponentSourceEvidenceReadiness,
    GeneratedTestSuitePolicy,
    GenerationContextBinding,
    GenerationExecutionPlan,
    GenerationFailure,
    GenerationOrchestrator,
    GenerationRequest,
    ModelStage,
)
from literate_ai.application.locked_generation_authority import (
    LockedGenerationAuthority,
)
from literate_ai.composition import ComponentComposer, FlavorResolver
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    Capability,
    ComponentCoordinate,
    ComponentDefinition,
    ComponentRevision,
    ContentIdentity,
    ContentReference,
    CycloneDxBomBinding,
    CycloneDxLifecycle,
    CycloneDxManagedGraph,
    FlavorAxis,
    FlavorCardinality,
    FlavorCoordinate,
    FlavorDefinition,
    FlavorSlot,
    HashAlgorithm,
    SourceIntelligenceArtifact,
    SpecificationArtifact,
    SpecificationRequirement,
    SpecificationScenario,
    SpecificationSet,
    TargetConstraint,
    TargetProfile,
    canonical_identity,
    canonical_json_bytes,
    project_component_lock_managed_graph,
)
from literate_ai.generated_tests import GENERATED_TEST_SUITE_SCHEMA
from literate_ai.models import (
    DataEgress,
    Locality,
    ModelEndpoint,
    ModelGroup,
    ModelRouter,
    StageModelPolicy,
)
from literate_ai.ports import (
    AUTHORIZED_EXECUTION_PROFILE,
    NON_EXECUTING_EXACT_TREE_PROFILE,
)
from literate_ai.registry import (
    ComponentDescriptor,
    DescriptorRegistry,
    FlavorDescriptor,
)
from literate_ai.security import (
    BuildRequestDeclaration,
    ObservationExecutionAuthorization,
    ObservationRequest,
    SecurityProfile,
)
from tests.support.fixtures_test_component_lock_contracts import component_lock

TEST_RECIPE_IDENTITY = "sha256:" + "a" * 64
TEST_SPECIFICATION_REFERENCES = ("spec.md",)
SOURCE_SBOM_CONTENT = "{}"


def generated_suite_content(*, recipe_identity: str = TEST_RECIPE_IDENTITY) -> str:
    return canonical_json_bytes(
        {
            "schema": GENERATED_TEST_SUITE_SCHEMA,
            "recipe_identity": recipe_identity,
            "generation_mode": "major-rebuild",
            "cases": [
                {
                    "case_id": "example",
                    "category": "example",
                    "specification_refs": ["spec.md"],
                    "arguments": [1],
                    "expected_result": 1,
                },
                {
                    "case_id": "boundary",
                    "category": "boundary",
                    "specification_refs": ["spec.md"],
                    "arguments": [0],
                    "expected_result": 0,
                },
                {
                    "case_id": "invariant",
                    "category": "invariant",
                    "specification_refs": ["spec.md"],
                    "arguments": [-1],
                    "expected_result": -1,
                },
            ],
        }
    ).decode("utf-8")


def identity(label: str):
    return canonical_identity({"fixture": label})


def reference(kind: str, label: str) -> ContentReference:
    return ContentReference(kind, f"fixture://{label}", identity(label))


def fixture(
    *, has_source_snapshot: bool = False
) -> tuple[
    ComponentRevision,
    TargetProfile,
    ComponentComposer,
    FlavorResolver,
]:
    slot = FlavorSlot(
        "language",
        FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
        FlavorCardinality.EXACTLY_ONE,
        "language",
    )
    definition = ComponentDefinition(
        ComponentCoordinate("test", "application"),
        "1.0.0",
        "Application",
        "neutral fixture",
        ("application",),
        False,
        (Capability("app.api", "1.0.0"),),
        (),
        "openspec",
        ("openspec",),
        (),
        reference("workflow", "generation"),
        reference("routing-policy", "models"),
        (slot,),
        (),
        (),
    )
    specifications = SpecificationSet(
        "openspec",
        "1",
        (SpecificationArtifact("spec.md", identity("spec")),),
        (
            SpecificationRequirement(
                "behavior",
                "Behavior",
                "The program shall run.",
                (SpecificationScenario("run", "Run", (), ("started",), ("runs",)),),
            ),
        ),
    )
    base = ComponentRevision(
        definition,
        specifications,
        identity("existing-source") if has_source_snapshot else None,
        (),
        (),
    )
    flavor_definition = FlavorDefinition(
        FlavorCoordinate("test", "python"),
        "1.0.0",
        "Python",
        FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
        (),
        ("app.api",),
        (),
        (),
        (reference("specification", "python-spec"),),
        (),
        (),
        (),
        (),
        (),
        (),
        ("python",),
    )
    flavor = FlavorDescriptor(identity("python-flavor"), flavor_definition, "python")
    registry = DescriptorRegistry(
        (ComponentDescriptor(base.identity, definition),), (flavor,)
    )
    target = TargetProfile(
        "python",
        "1.0.0",
        "explicit",
        identity("target-provider"),
        (TargetConstraint(FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM, "python"),),
    )
    return base, target, ComponentComposer(registry), FlavorResolver(registry)


class Readiness:
    def __init__(self, ready: bool = True, source_available: bool = True) -> None:
        self.ready = ready
        self.source_available = source_available
        self.calls = 0

    def inspect(self, authority, component_revision):
        self.calls += 1
        assert any(
            item.revision.identity == component_revision
            for item in authority.lock.nodes
        )
        return ComponentSourceEvidenceReadiness(
            component_revision,
            (identity("source").uri,) if self.ready and self.source_available else (),
            (identity("evidence").uri,) if self.ready else (),
            self.ready,
            self.ready,
            () if self.ready else ("source", "evidence"),
        )


class Models:
    provider_id = "test-models"

    def __init__(
        self,
        calls: list[str],
        fail_generate_once: bool = False,
        include_test_suite: bool = True,
        suite_recipe_identity: str = TEST_RECIPE_IDENTITY,
    ) -> None:
        self.calls = calls
        self.fail_generate_once = fail_generate_once
        self.include_test_suite = include_test_suite
        self.suite_recipe_identity = suite_recipe_identity
        self.generated_files: Mapping[str, str] | None = None
        self.requests: list[Mapping[str, object]] = []

    def complete_structured(self, request):
        self.requests.append(request)
        stage = str(request["stage_id"])
        self.calls.append(f"model:{stage}")
        if stage == "generate" and self.fail_generate_once:
            self.fail_generate_once = False
            raise RuntimeError("transient model failure")
        if stage == "plan":
            return {"plan": ["source/main.py"]}
        files = dict(
            self.generated_files
            if self.generated_files is not None
            else {"source/main.py": "print('ok')\n"}
        )
        if self.include_test_suite:
            files["source/tests/manifest.json"] = generated_suite_content(
                recipe_identity=self.suite_recipe_identity
            )
        files[CYCLONEDX_SOURCE_SBOM_PATH] = SOURCE_SBOM_CONTENT
        return {"files": files}


class Validator:
    def __init__(self, calls: list[str], passed: bool = True) -> None:
        self.calls = calls
        self.passed = passed

    def validate(self, artifact):
        self.calls.append("validate")
        return {
            "passed": self.passed,
            "findings": [] if self.passed else [{"code": "invalid"}],
        }


class Classifier:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def classify(self, source, findings: Sequence[Mapping[str, object]]):
        self.calls.append("classify")
        return {
            "classification_digest": identity("classification").uri,
            "effective_revision_digest": source["effective_revision_digest"],
            "source_digests": [source["source_bundle_digest"]],
            "profile": "constrained",
            "finding_count": len(findings),
        }


class Authorizer:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def authorize(self, classification, build_request):
        self.calls.append("authorize")
        return {
            "authorization_id": "authorization:test",
            "classification_digest": classification["classification_digest"],
            "request_digest": canonical_identity(build_request).uri,
            "effective_revision_digest": build_request["effective_revision_digest"],
            "profile": "constrained",
        }


class Builder:
    builder_id = "test-builder"

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def build(self, request, authorization):
        self.calls.append("build")
        assert request["artifact"]["source_sbom_path"] == CYCLONEDX_SOURCE_SBOM_PATH
        return {
            "artifact_digest": identity("build").uri,
            "source_bundle_digest": request["source_bundle_digest"],
            "authorization_id": authorization["authorization_id"],
            "compiled_files": ["main.pyc"],
        }


class DependencyResolver:
    resolver_id = "test-dependency-resolver"

    def __init__(
        self,
        calls: list[str],
        managed_graph: CycloneDxManagedGraph,
        *,
        source_valid: bool = True,
        resolution_authority_valid: bool = True,
    ) -> None:
        self.calls = calls
        self.managed_graph = managed_graph
        self.source_valid = source_valid
        self.resolution_authority_valid = resolution_authority_valid

    def validate_source(self, artifact):
        assert CYCLONEDX_SOURCE_SBOM_PATH in artifact["files"]
        if not self.source_valid:
            self.calls.append("reconcile-dependencies")
            raise ValueError("fixture dependency reconciliation failed")
        source = self._source_binding(artifact)
        material = {
            "resolver_id": self.resolver_id,
            "effective_revision_digest": artifact["effective_revision_digest"],
            "source_bundle_digest": artifact["source_bundle_digest"],
            "source_bom": source.to_dict(),
        }
        return {
            **material,
            "validation_identity": canonical_identity(material).uri,
        }

    def resolve(self, artifact, build):
        self.calls.append("resolve-dependencies")
        source_validation = self.validate_source(artifact)
        source = CycloneDxBomBinding.from_dict(source_validation["source_bom"])
        if not self.resolution_authority_valid:
            source = CycloneDxBomBinding(
                source.lifecycle,
                source.bom_identity,
                source.graph_identity,
                identity("fabricated-managed-graph"),
                identity("fabricated-component-composition"),
                source.source_bom_identity,
                "urn:fixture:fabricated-root",
                source.component_count,
                source.edge_count,
            )
        resolved = CycloneDxBomBinding(
            CycloneDxLifecycle.RESOLVED,
            identity("resolved-bom"),
            identity("resolved-graph"),
            source.managed_graph_identity,
            source.resolved_graph_identity,
            source.bom_identity,
            source.root_ref,
            source.component_count,
            source.edge_count,
        )
        result = {
            "resolver_id": self.resolver_id,
            "effective_revision_digest": artifact["effective_revision_digest"],
            "source_bundle_digest": artifact["source_bundle_digest"],
            "artifact_digest": build["artifact_digest"],
            "source_validation_identity": source_validation["validation_identity"],
            "source_bom": source.to_dict(),
            "resolved_bom": resolved.to_dict(),
            "repository_resolutions": [],
        }
        result["resolution_identity"] = canonical_identity(result).uri
        return result

    def _source_binding(self, artifact):
        source_identity = type(identity("source-bom"))(
            HashAlgorithm.SHA256,
            hashlib.sha256(SOURCE_SBOM_CONTENT.encode("utf-8")).hexdigest(),
        )
        return CycloneDxBomBinding(
            CycloneDxLifecycle.SOURCE,
            source_identity,
            identity("source-graph"),
            self.managed_graph.identity,
            self.managed_graph.resolved_graph_identity,
            None,
            self.managed_graph.root_ref,
            len(self.managed_graph.components),
            len(self.managed_graph.edges),
        )


class GeneratedTestRunner:
    runner_id = "test-generated-runner"

    def __init__(
        self,
        calls: list[str],
        passed: bool = True,
        suite_identity_override: str | None = None,
    ) -> None:
        self.calls = calls
        self.passed = passed
        self.suite_identity_override = suite_identity_override

    def run(self, artifact, build, classification, dependency_resolution, test_suite):
        self.calls.append("test-generated")
        assert dependency_resolution["artifact_digest"] == build["artifact_digest"]
        total = len(test_suite.case_ids)
        passed_count = total if self.passed else 0
        failed_count = 0 if self.passed else total
        return {
            "passed": self.passed,
            "runner_id": self.runner_id,
            "execution_profile": NON_EXECUTING_EXACT_TREE_PROFILE,
            "profile_reason": "unit fixture checks immutable identities only",
            "classification_digest": classification["classification_digest"],
            "effective_revision_digest": artifact["effective_revision_digest"],
            "source_bundle_digest": artifact["source_bundle_digest"],
            "artifact_digest": build["artifact_digest"],
            "dependency_resolution_identity": dependency_resolution[
                "resolution_identity"
            ],
            "verified_tree_identity": artifact["tree_identity"],
            "test_suite_identity": (
                self.suite_identity_override or test_suite.content_identity
            ),
            "total": total,
            "passed_count": passed_count,
            "failed_count": failed_count,
            "skipped_count": 0,
            "case_results": [
                {
                    "case_id": case_id,
                    "passed": self.passed,
                    "classification_digest": classification["classification_digest"],
                }
                for case_id in test_suite.case_ids
            ],
        }


class IndependentAcceptanceRunner:
    runner_id = "test-independent-acceptance-runner"
    acceptance_suite_identity = identity("independent-acceptance-suite").uri

    def __init__(
        self,
        calls: list[str],
        passed: bool = True,
        execution_profile: str = AUTHORIZED_EXECUTION_PROFILE,
    ) -> None:
        self.calls = calls
        self.passed = passed
        self.execution_profile = execution_profile

    def run(
        self,
        artifact,
        build,
        classification,
        generated_tests,
        dependency_resolution,
    ):
        self.calls.append("verify-independent")
        assert dependency_resolution["artifact_digest"] == build["artifact_digest"]
        passed_count = 1 if self.passed else 0
        failed_count = 0 if self.passed else 1
        case_result = {
            "case_id": "pinned-primary",
            "passed": self.passed,
            "generated_tests_identity": canonical_identity(generated_tests).uri,
        }
        if self.execution_profile == AUTHORIZED_EXECUTION_PROFILE:
            observation = ObservationRequest(
                artifact["effective_revision_digest"],
                (artifact["source_bundle_digest"],),
                "runner:unit-acceptance@1",
                identity("unit-acceptance-harness").uri,
                "unit-fixture",
                (),
                ("decision",),
            )
            authorization = ObservationExecutionAuthorization(
                "observation-authorization:unit",
                classification["classification_digest"],
                canonical_identity(observation.to_dict()).uri,
                artifact["effective_revision_digest"],
                "unit-test",
                "exercise the authorized acceptance contract",
                SecurityProfile.CONSTRAINED,
                (),
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=5),
            )
            case_result["observation_request"] = observation.to_dict()
            case_result["execution_authorization"] = authorization.to_dict()
        return {
            "passed": self.passed,
            "runner_id": self.runner_id,
            "execution_profile": self.execution_profile,
            **(
                {"profile_reason": "unit fixture checks immutable identities only"}
                if self.execution_profile == NON_EXECUTING_EXACT_TREE_PROFILE
                else {}
            ),
            "classification_digest": classification["classification_digest"],
            "effective_revision_digest": artifact["effective_revision_digest"],
            "source_bundle_digest": artifact["source_bundle_digest"],
            "artifact_digest": build["artifact_digest"],
            "dependency_resolution_identity": dependency_resolution[
                "resolution_identity"
            ],
            "verified_tree_identity": artifact["tree_identity"],
            "test_suite_identity": self.acceptance_suite_identity,
            "total": 1,
            "passed_count": passed_count,
            "failed_count": failed_count,
            "skipped_count": 0,
            "case_results": [case_result],
        }


class Workspace:
    def __init__(self, calls: list[str], fail_commit_once: bool = False) -> None:
        self.calls = calls
        self.fail_commit_once = fail_commit_once

    def prepare(self, files, dependency_resolution):
        self.calls.append("prepare")
        manifest = [
            {
                "path": path,
                "size": len(content),
                "digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
            }
            for path, content in sorted(files.items())
        ]
        return {
            "tree_digest": canonical_identity(manifest).uri,
            "manifest": [[item["path"], item["digest"]] for item in manifest],
            "dependency_resolution_identity": dependency_resolution[
                "resolution_identity"
            ],
            "source_bom_binding_identity": canonical_identity(
                dependency_resolution["source_bom"]
            ).uri,
            "resolved_bom_binding_identity": canonical_identity(
                dependency_resolution["resolved_bom"]
            ).uri,
        }

    def commit(self, prepared, reference, dependency_resolution):
        self.calls.append("commit")
        if self.fail_commit_once:
            self.fail_commit_once = False
            raise RuntimeError("interrupted atomic reference update")
        source_intelligence = SourceIntelligenceArtifact.create(
            provider_id="fixture-intelligence",
            provider_version="test-fixture-v1",
            runtime_version="1.1.1",
            executable_identity="sha256:" + "e" * 64,
            source_tree_identity=prepared["tree_digest"],
            source_snapshot_identity=canonical_identity(
                {"source_tree_identity": prepared["tree_digest"]}
            ).uri,
            capabilities=(),
            provider_properties=(),
            artifact_path="source-intelligence.sqlite",
            artifact_media_type="application/vnd.sqlite3",
            artifact_identity="sha256:" + "d" * 64,
            document_count=0,
            symbol_count=0,
            relationship_count=0,
            unresolved_relationship_count=0,
            warning_count=0,
        )
        return {
            "tree_digest": prepared["tree_digest"],
            "reference": reference,
            "path": f"/accepted/{reference}",
            "source_intelligence": source_intelligence.to_dict(),
            "source_intelligence_status": {
                "schema": "literate-ai/source-intelligence-stage-status@1",
                "stage": "source-generation",
                "mode": "required",
                "state": "current",
                "provider_id": "fixture-intelligence",
            },
            "dependency_resolution_identity": dependency_resolution[
                "resolution_identity"
            ],
            "source_bom_binding_identity": canonical_identity(
                dependency_resolution["source_bom"]
            ).uri,
            "resolved_bom_binding_identity": canonical_identity(
                dependency_resolution["resolved_bom"]
            ).uri,
        }

    def recover(self):
        return ()


class Events:
    def __init__(self) -> None:
        self.records: list[tuple[str, Mapping[str, object]]] = []

    def append(self, stream_id, event):
        self.records.append((stream_id, event))

    def stream(self, stream_id):
        return tuple(event for stream, event in self.records if stream == stream_id)


def router() -> ModelRouter:
    endpoint = ModelEndpoint(
        "local",
        "test",
        "fixture-model",
        "http://127.0.0.1:9999",
        Locality.LOCAL,
        ("structured",),
        10000,
    )
    return ModelRouter(
        endpoints=(endpoint,),
        groups=(ModelGroup("generation", "1.0.0", ("local",)),),
    )


def stages() -> tuple[ModelStage, ...]:
    return (
        ModelStage(
            "plan",
            StageModelPolicy(
                "plan-policy", "plan", "generation", data_egress=DataEgress.NONE
            ),
        ),
        ModelStage(
            "generate",
            StageModelPolicy(
                "generate-policy",
                "generate",
                "generation",
                data_egress=DataEgress.NONE,
            ),
            dependencies=("plan",),
            produces_tree=True,
        ),
    )


def locked_authority_fixture() -> LockedGenerationAuthority:
    lock = component_lock()
    root_node = next(
        item for item in lock.nodes if item.revision.identity == lock.root_revision
    )
    root_authoring = next(
        item
        for item in lock.authorings
        if item.identity == root_node.revision.authoring_identity
    )
    return LockedGenerationAuthority(lock, root_authoring, lock.authorings, (), ())


def execution_plan(authority: LockedGenerationAuthority) -> GenerationExecutionPlan:
    stage_values = stages()
    route_selector = router()
    root = next(
        item.revision
        for item in authority.lock.nodes
        if item.revision.identity == authority.lock.root_revision
    )
    return GenerationExecutionPlan(
        root.workflow_definition,
        root.routing_policy,
        stage_values,
        tuple(route_selector.select(item.policy) for item in stage_values),
    )


def generation_context(
    authority: LockedGenerationAuthority,
    plan: GenerationExecutionPlan,
    *,
    component_revision=None,
    workspace_reference: str = "generated/application",
) -> GenerationContextBinding:
    selected = component_revision or authority.lock.root_revision
    prompt = plan.model_stages[0].instructions
    assert all(stage.instructions.endswith(prompt) for stage in plan.model_stages)
    return GenerationContextBinding(
        component_revision=selected,
        component_generation_plan_identity=canonical_identity(
            {"lock": authority.lock.identity.uri, "component": selected.uri}
        ),
        generation_key_identity=canonical_identity(
            {"component": selected.uri, "execution_plan": plan.identity.uri}
        ),
        context_manifest_identity=canonical_identity(
            {"component": selected.uri, "prompt": prompt}
        ),
        complexity_decision_identity=canonical_identity(
            {"context": selected.uri, "allowed": True}
        ),
        prompt_identity=ContentIdentity.parse_uri(
            "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        ),
        generation_recipe_identity=ContentIdentity.parse_uri(TEST_RECIPE_IDENTITY),
        workspace_allocation_identity=canonical_identity(
            {"component": selected.uri, "workspace": workspace_reference}
        ),
        workspace_reference=workspace_reference,
        prompt=prompt,
    )


class GenerationOrchestratorTests(unittest.TestCase):
    def make_system(
        self,
        *,
        ready: bool = True,
        valid: bool = True,
        fail_generate_once: bool = False,
        fail_commit_once: bool = False,
        generated_tests_passed: bool = True,
        acceptance_passed: bool = True,
        acceptance_execution_profile: str = AUTHORIZED_EXECUTION_PROFILE,
        include_test_suite: bool = True,
        generated_suite_recipe_identity: str = TEST_RECIPE_IDENTITY,
        generated_suite_identity_override: str | None = None,
        has_source_snapshot: bool = False,
        source_available: bool = True,
        dependency_source_valid: bool = True,
        dependency_resolution_authority_valid: bool = True,
        component_revision: ContentIdentity | None = None,
    ):
        del has_source_snapshot
        authority = locked_authority_fixture()
        selected_revision = component_revision or authority.lock.root_revision
        managed_graph = project_component_lock_managed_graph(
            authority.lock, selected_revision
        )
        plan = execution_plan(authority)
        request = GenerationRequest(
            locked_authority=authority,
            component_revision=selected_revision,
            generation_context=generation_context(
                authority, plan, component_revision=selected_revision
            ),
            execution_plan=plan,
            build_request_declaration=BuildRequestDeclaration(
                effective_revision_digest=selected_revision.uri,
                builder_id="test-builder",
                toolchain_digest=identity("test-toolchain").uri,
                sandbox_profile="test-sandbox",
                requested_privileges=(),
                allowed_outputs=("test-artifact",),
            ),
            workspace_reference="generated/application",
            generated_test_suite_policy=GeneratedTestSuitePolicy(
                TEST_RECIPE_IDENTITY,
                TEST_SPECIFICATION_REFERENCES,
            ),
            managed_sbom_graph=managed_graph,
        )
        calls: list[str] = []
        readiness = Readiness(ready, source_available)
        models = Models(
            calls,
            fail_generate_once,
            include_test_suite,
            generated_suite_recipe_identity,
        )
        workspace = Workspace(calls, fail_commit_once)
        events = Events()
        orchestrator = GenerationOrchestrator(
            readiness_provider=readiness,
            model_provider=models,
            validator=Validator(calls, valid),
            classifier=Classifier(calls),
            build_authorizer=Authorizer(calls),
            builder=Builder(calls),
            dependency_resolver=DependencyResolver(
                calls,
                managed_graph,
                source_valid=dependency_source_valid,
                resolution_authority_valid=(dependency_resolution_authority_valid),
            ),
            generated_test_runner=GeneratedTestRunner(
                calls,
                passed=generated_tests_passed,
                suite_identity_override=generated_suite_identity_override,
            ),
            acceptance_runner=IndependentAcceptanceRunner(
                calls,
                passed=acceptance_passed,
                execution_profile=acceptance_execution_profile,
            ),
            workspace=workspace,
            event_store=events,
        )
        return request, orchestrator, calls, readiness, models, events

    @staticmethod
    def for_component(
        request: GenerationRequest, component_revision: ContentIdentity
    ) -> GenerationRequest:
        build_request_declaration = replace(
            request.build_request_declaration,
            effective_revision_digest=component_revision.uri,
        )
        return replace(
            request,
            component_revision=component_revision,
            generation_context=generation_context(
                request.locked_authority,
                request.execution_plan,
                component_revision=component_revision,
                workspace_reference=request.workspace_reference,
            ),
            build_request_declaration=build_request_declaration,
            managed_sbom_graph=project_component_lock_managed_graph(
                request.locked_authority.lock, component_revision
            ),
        )

    def test_failure_events_never_persist_exception_messages_or_secrets(self) -> None:
        secret = "forwarded-secret-must-not-enter-event-history"
        request, orchestrator, _, _, _, events = self.make_system()

        class SecretBearingFailureModel:
            provider_id = "secret-bearing-failure-fixture"

            def complete_structured(self, request):
                raise RuntimeError(secret)

        orchestrator.model_provider = SecretBearingFailureModel()

        with self.assertRaises(GenerationFailure) as rejected:
            orchestrator.execute(request)

        failed = rejected.exception.run
        assert failed is not None
        self.assertEqual(failed.events[-1].event_type, "run-failed")
        self.assertEqual(failed.events[-1].data["error_type"], "RuntimeError")
        for event in failed.events:
            serialized = event.to_dict()
            self.assertNotIn("message", event.data)
            self.assertNotIn(secret, json.dumps(serialized, sort_keys=True))

        persisted = [event for _, event in events.records]
        self.assertEqual(persisted[-1]["data"]["error_type"], "RuntimeError")
        for event in persisted:
            data = event["data"]
            assert isinstance(data, Mapping)
            self.assertNotIn("message", data)
            self.assertNotIn(secret, json.dumps(event, sort_keys=True))

    def test_generated_test_failure_prevents_workspace_preparation_and_commit(
        self,
    ) -> None:
        request, orchestrator, calls, _, _, _ = self.make_system(
            generated_tests_passed=False
        )

        with self.assertRaises(GenerationFailure) as rejected:
            orchestrator.execute(request)

        self.assertEqual(rejected.exception.code, "generation.generated-tests-rejected")
        self.assertEqual(
            calls,
            [
                "model:plan",
                "model:generate",
                "validate",
                "classify",
                "authorize",
                "build",
                "resolve-dependencies",
                "test-generated",
            ],
        )
        self.assertNotIn("prepare", calls)
        self.assertNotIn("commit", calls)

    def test_independent_acceptance_failure_prevents_workspace_truth(self) -> None:
        request, orchestrator, calls, _, _, _ = self.make_system(
            acceptance_passed=False
        )

        with self.assertRaises(GenerationFailure) as rejected:
            orchestrator.execute(request)

        self.assertEqual(rejected.exception.code, "generation.acceptance-rejected")
        self.assertEqual(calls[-1], "verify-independent")
        self.assertNotIn("prepare", calls)
        self.assertNotIn("commit", calls)

    def test_resume_rejects_tampered_realized_build_request(self) -> None:
        request, orchestrator, *_ = self.make_system()
        paused = orchestrator.execute(request, stop_after="generate")
        assert paused.realized_build_request is not None
        tampered = replace(
            paused,
            realized_build_request=replace(
                paused.realized_build_request,
                toolchain_digest=identity("tampered-toolchain").uri,
            ),
        )

        with self.assertRaises(GenerationFailure) as rejected:
            orchestrator.execute(request, resume=tampered)

        self.assertEqual(
            rejected.exception.code, "generation.resume-checkpoint-invalid"
        )


if __name__ == "__main__":
    unittest.main()
