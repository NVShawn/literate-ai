"""Immutable application-layer generation inputs, state, and provenance."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import Any

from literate_ai.application.locked_generation_authority import (
    LockedGenerationAuthority,
)
from literate_ai.contracts import (
    ContentIdentity,
    ContentReference,
    CycloneDxManagedGraph,
    canonical_identity,
    canonical_json_bytes,
    project_component_lock_managed_graph,
)
from literate_ai.models import ModelRouteDecision, StageModelPolicy
from literate_ai.ports.contracts import (
    AUTHORIZED_EXECUTION_PROFILE,
    NON_EXECUTING_EXACT_TREE_PROFILE,
)
from literate_ai.security import BuildRequest, BuildRequestDeclaration

DEFAULT_ACCEPTANCE_POLICY_ID = "literate-ai/authorized-independent-acceptance@1"
SELF_HOST_EXACT_TREE_POLICY_ID = "literate-ai/self-host-exact-tree-replay@1"
SELF_HOST_EXACT_TREE_ACCEPTANCE_RUNNER_ID = "acceptance:exact-snapshot-tree@1"


class GenerationStatus(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True, init=False)
class GeneratedTestSuitePolicy:
    """Exact recipe inputs used to admit one generated test-suite artifact."""

    recipe_identity: str
    specification_references: tuple[str, ...]
    _acceptance_arguments_json: bytes

    def __init__(
        self,
        recipe_identity: str,
        specification_references: tuple[str, ...],
        acceptance_arguments: tuple[object, ...] = (),
    ) -> None:
        ContentIdentity.parse_uri(recipe_identity)
        references = tuple(specification_references)
        if (
            not references
            or any(not isinstance(item, str) or not item.strip() for item in references)
            or len(set(references)) != len(references)
        ):
            raise ValueError(
                "generated-test specification references must be unique non-empty "
                "strings"
            )
        arguments = tuple(acceptance_arguments)
        if any(not isinstance(item, list) for item in arguments):
            raise ValueError("acceptance arguments must be JSON argument arrays")
        encoded = canonical_json_bytes(list(arguments))
        object.__setattr__(self, "recipe_identity", recipe_identity)
        object.__setattr__(self, "specification_references", references)
        object.__setattr__(self, "_acceptance_arguments_json", encoded)

    @property
    def acceptance_arguments(self) -> tuple[object, ...]:
        value = json.loads(self._acceptance_arguments_json)
        assert isinstance(value, list)
        return tuple(value)

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "recipe_identity": self.recipe_identity,
            "specification_references": list(self.specification_references),
            "acceptance_arguments": list(self.acceptance_arguments),
        }


@dataclass(frozen=True, slots=True)
class IndependentAcceptancePolicy:
    """Profile and optional exact verifier pins for independent acceptance."""

    policy_id: str = DEFAULT_ACCEPTANCE_POLICY_ID
    execution_profile: str = AUTHORIZED_EXECUTION_PROFILE
    runner_id: str | None = None
    suite_identity: str | None = None

    def __post_init__(self) -> None:
        if not self.policy_id.strip():
            raise ValueError("independent acceptance policy ID cannot be empty")
        if self.runner_id is not None and not self.runner_id.strip():
            raise ValueError("independent acceptance runner pin cannot be empty")
        if self.suite_identity is not None:
            ContentIdentity.parse_uri(self.suite_identity)
        if self.execution_profile == AUTHORIZED_EXECUTION_PROFILE:
            if self.policy_id == SELF_HOST_EXACT_TREE_POLICY_ID:
                raise ValueError(
                    "the self-host exact-tree policy cannot authorize code"
                )
            return
        if self.execution_profile != NON_EXECUTING_EXACT_TREE_PROFILE:
            raise ValueError("independent acceptance profile is unsupported")
        if (
            self.policy_id != SELF_HOST_EXACT_TREE_POLICY_ID
            or self.runner_id != SELF_HOST_EXACT_TREE_ACCEPTANCE_RUNNER_ID
            or self.suite_identity is None
        ):
            raise ValueError(
                "non-executing exact-tree acceptance is reserved for the exact "
                "self-host replay policy, verifier, and suite"
            )

    @classmethod
    def self_host_exact_tree(cls, suite_identity: str) -> IndependentAcceptancePolicy:
        return cls(
            policy_id=SELF_HOST_EXACT_TREE_POLICY_ID,
            execution_profile=NON_EXECUTING_EXACT_TREE_PROFILE,
            runner_id=SELF_HOST_EXACT_TREE_ACCEPTANCE_RUNNER_ID,
            suite_identity=suite_identity,
        )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "execution_profile": self.execution_profile,
            "runner_id": self.runner_id,
            "suite_identity": self.suite_identity,
        }

    def require_runner(self, *, runner_id: str, suite_identity: str) -> None:
        if not isinstance(runner_id, str) or not runner_id.strip():
            raise ValueError("independent acceptance runner ID cannot be empty")
        ContentIdentity.parse_uri(suite_identity)
        if self.runner_id is not None and self.runner_id != runner_id:
            raise ValueError("independent acceptance policy targets another runner")
        if self.suite_identity is not None and self.suite_identity != suite_identity:
            raise ValueError("independent acceptance policy targets another suite")


@dataclass(frozen=True, slots=True)
class ModelStage:
    stage_id: str
    policy: StageModelPolicy
    dependencies: tuple[str, ...] = ()
    produces_tree: bool = False
    instructions: str = (
        "Produce the declared structured output from exact supplied evidence."
    )
    response_schema_name: str = "stage_output"
    response_schema: Mapping[str, object] = field(
        default_factory=lambda: {"type": "object"}
    )
    content_kind: str = "source"
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if not self.stage_id:
            raise ValueError("model stage ID cannot be empty")
        if self.policy.stage_type != self.stage_id:
            raise ValueError("model stage ID must equal its policy stage type")
        if (
            self.stage_id in self.dependencies
            or len(set(self.dependencies)) != len(self.dependencies)
            or any(not item for item in self.dependencies)
        ):
            raise ValueError("model stage dependencies must be unique earlier stages")
        if not self.instructions.strip() or not self.response_schema_name.strip():
            raise ValueError("model stage instructions and schema name cannot be empty")
        if self.response_schema.get("type") != "object":
            raise ValueError("model stage response schema root must be an object")
        if self.content_kind not in {"metadata", "source"}:
            raise ValueError("model stage content kind must be metadata or source")
        if self.max_output_tokens is not None and self.max_output_tokens < 1:
            raise ValueError("model stage output-token limit must be positive")
        canonical_json_bytes(dict(self.response_schema))


FULL_GENERATION_LIFECYCLE_STEPS = (
    "validate",
    "classify",
    "authorize-build",
    "build",
    "resolve-dependencies",
    "test-generated",
    "verify-independent",
    "prepare-tree",
    "commit-tree",
)


@dataclass(frozen=True, slots=True)
class GenerationExecutionPlan:
    """Exact workflow and routing decisions consumed by one generation run."""

    workflow_reference: ContentReference
    routing_reference: ContentReference
    model_stages: tuple[ModelStage, ...]
    route_decisions: tuple[ModelRouteDecision, ...]
    lifecycle_steps: tuple[str, ...] = FULL_GENERATION_LIFECYCLE_STEPS
    independent_acceptance_policy: IndependentAcceptancePolicy = field(
        default_factory=IndependentAcceptancePolicy
    )

    def __post_init__(self) -> None:
        if self.workflow_reference.kind != "workflow":
            raise ValueError("generation plan requires a workflow reference")
        if self.routing_reference.kind != "routing-policy":
            raise ValueError("generation plan requires a routing-policy reference")
        if not self.model_stages:
            raise ValueError("generation plan requires at least one model stage")
        if len(self.model_stages) != len(self.route_decisions):
            raise ValueError("every model stage requires one exact route decision")
        if len({item.stage_id for item in self.model_stages}) != len(self.model_stages):
            raise ValueError("generation plan model stage IDs must be unique")
        if len({item.policy_id for item in self.route_decisions}) != len(
            self.route_decisions
        ):
            raise ValueError("generation plan route decisions must be unique")
        if not isinstance(
            self.independent_acceptance_policy, IndependentAcceptancePolicy
        ):
            raise TypeError("generation plan requires a typed acceptance policy")
        tree_stages = [item for item in self.model_stages if item.produces_tree]
        if len(tree_stages) != 1 or tree_stages[0] is not self.model_stages[-1]:
            raise ValueError(
                "exactly the final model stage must produce the proposed tree"
            )
        earlier_stages: set[str] = set()
        for stage, route in zip(self.model_stages, self.route_decisions, strict=True):
            if not set(stage.dependencies).issubset(earlier_stages):
                raise ValueError(
                    "model stage dependencies must target earlier model stages"
                )
            if stage.policy.policy_id != route.policy_id:
                raise ValueError("model stage route decision targets another policy")
            if (
                route.group_ref is None
                or route.group_ref.identifier != stage.policy.group_id
                or route.group_digest != route.group_ref.content_identity.uri
            ):
                raise ValueError("model stage route decision targets another group")
            if (
                stage.policy.group_ref is not None
                and route.group_ref != stage.policy.group_ref
            ):
                raise ValueError("model stage route decision changed its exact group")
            if (
                route.selected_endpoint_ref is None
                or route.selected_endpoint_id != route.selected_endpoint_ref.identifier
                or route.selected_endpoint_digest
                != route.selected_endpoint_ref.content_identity.uri
            ):
                raise ValueError("model route requires an exact selected endpoint")
            if route.data_egress is not stage.policy.data_egress:
                raise ValueError("model route changed the stage data-egress policy")
            if route.fallback_used and not stage.policy.fallback_allowed:
                raise ValueError("model route used a disallowed fallback")
            earlier_stages.add(stage.stage_id)
        if self.lifecycle_steps != FULL_GENERATION_LIFECYCLE_STEPS:
            raise ValueError(
                "full generation plans require the canonical guarded lifecycle"
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "workflow_reference": self.workflow_reference.to_dict(),
            "routing_reference": self.routing_reference.to_dict(),
            "model_stages": [
                {
                    "stage_id": stage.stage_id,
                    "dependencies": list(stage.dependencies),
                    "policy_digest": stage.policy.digest,
                    "produces_tree": stage.produces_tree,
                    "instructions_digest": canonical_identity(stage.instructions).uri,
                    "response_schema_name": stage.response_schema_name,
                    "response_schema": dict(stage.response_schema),
                    "content_kind": stage.content_kind,
                    "max_output_tokens": stage.max_output_tokens,
                }
                for stage in self.model_stages
            ],
            "route_decisions": [
                route_decision_dict(route) for route in self.route_decisions
            ],
            "lifecycle_steps": list(self.lifecycle_steps),
            "independent_acceptance_policy": (
                self.independent_acceptance_policy.to_dict()
            ),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class GenerationRequest:
    locked_authority: LockedGenerationAuthority
    component_revision: ContentIdentity
    generation_context: GenerationContextBinding
    execution_plan: GenerationExecutionPlan
    build_request_declaration: BuildRequestDeclaration
    workspace_reference: str
    generated_test_suite_policy: GeneratedTestSuitePolicy
    managed_sbom_graph: CycloneDxManagedGraph

    def __post_init__(self) -> None:
        if not isinstance(self.locked_authority, LockedGenerationAuthority):
            raise TypeError("generation requires exact locked generation authority")
        if not isinstance(self.component_revision, ContentIdentity):
            raise TypeError("generation requires an exact Component revision")
        if not isinstance(self.generation_context, GenerationContextBinding):
            raise TypeError("generation requires an exact bounded context binding")
        if not isinstance(self.execution_plan, GenerationExecutionPlan):
            raise TypeError("generation requires a typed execution plan")
        if not isinstance(self.build_request_declaration, BuildRequestDeclaration):
            raise TypeError("generation requires a typed build request declaration")
        if not isinstance(self.generated_test_suite_policy, GeneratedTestSuitePolicy):
            raise TypeError("generation requires a typed generated-test suite policy")
        if not isinstance(self.managed_sbom_graph, CycloneDxManagedGraph):
            raise TypeError("generation requires an exact managed SBOM graph")
        lock = self.locked_authority.lock
        selected = tuple(
            item.revision
            for item in lock.nodes
            if item.revision.identity == self.component_revision
        )
        if len(selected) != 1:
            raise ValueError(
                "generation Component revision must be an exact node in the lock"
            )
        component = selected[0]
        if self.managed_sbom_graph != project_component_lock_managed_graph(
            lock, self.component_revision
        ):
            raise ValueError(
                "generation managed SBOM graph does not target the exact locked "
                "Component closure"
            )
        if (
            self.execution_plan.workflow_reference != component.workflow_definition
            or self.execution_plan.routing_reference != component.routing_policy
        ):
            raise ValueError(
                "generation plan does not match the locked Component's workflow "
                "and routing"
            )
        if (
            self.generation_context.component_revision != self.component_revision
            or self.generated_test_suite_policy.recipe_identity
            != self.generation_context.generation_recipe_identity.uri
        ):
            raise ValueError(
                "generation context does not bind the selected Component and recipe"
            )
        if any(
            self.generation_context.prompt not in stage.instructions
            for stage in self.execution_plan.model_stages
        ):
            raise ValueError(
                "generation plan does not contain the exact bounded prompt"
            )
        if not self.workspace_reference:
            raise ValueError("generation requires a workspace reference")
        if self.generation_context.workspace_reference != self.workspace_reference:
            raise ValueError("generation context targets another workspace reference")
        if (
            self.build_request_declaration.effective_revision_digest
            != self.component_revision.uri
        ):
            raise ValueError(
                "generation build declaration does not target the selected Component"
            )

    @property
    def model_stages(self) -> tuple[ModelStage, ...]:
        """Compatibility view; execution semantics live in ``execution_plan``."""

        return self.execution_plan.model_stages


@dataclass(frozen=True, slots=True)
class GenerationContextBinding:
    """Non-authority bridge from a bounded node request into orchestration.

    The prompt is the only caller-supplied authority content this contract permits the
    model adapter to receive.  All other fields are immutable identities used to bind
    that prompt to its Component plan, recipe, and fresh workspace.
    """

    component_revision: ContentIdentity
    component_generation_plan_identity: ContentIdentity
    generation_key_identity: ContentIdentity
    context_manifest_identity: ContentIdentity
    complexity_decision_identity: ContentIdentity
    prompt_identity: ContentIdentity
    generation_recipe_identity: ContentIdentity
    workspace_allocation_identity: ContentIdentity
    workspace_reference: str
    prompt: str

    SCHEMA = "urn:literate-ai:schema:v3:generation-context-binding"

    def __post_init__(self) -> None:
        for name in (
            "component_revision",
            "component_generation_plan_identity",
            "generation_key_identity",
            "context_manifest_identity",
            "complexity_decision_identity",
            "prompt_identity",
            "generation_recipe_identity",
            "workspace_allocation_identity",
        ):
            if not isinstance(getattr(self, name), ContentIdentity):
                raise TypeError(f"generation context {name} must be ContentIdentity")
        if not isinstance(self.prompt, str) or not self.prompt:
            raise ValueError("generation context prompt must be non-empty UTF-8 text")
        if (
            not isinstance(self.workspace_reference, str)
            or not self.workspace_reference
        ):
            raise ValueError("generation context workspace reference must be non-empty")
        digest = hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()
        if self.prompt_identity != ContentIdentity.parse_uri(f"sha256:{digest}"):
            raise ValueError(
                "generation context prompt identity does not match its bytes"
            )

    @classmethod
    def from_prepared(
        cls,
        prepared: object,
        *,
        component_revision: ContentIdentity,
        generation_recipe_identity: ContentIdentity,
        workspace_allocation_identity: ContentIdentity,
        workspace_reference: str,
    ) -> GenerationContextBinding:
        from literate_ai.application.component_generation_context import (
            PreparedComponentGenerationRequest,
        )

        if not isinstance(prepared, PreparedComponentGenerationRequest):
            raise TypeError("prepared must be a PreparedComponentGenerationRequest")
        request = prepared.request
        try:
            prompt = prepared.prompt.decode("utf-8")
        except UnicodeError as error:
            raise ValueError("bounded generation prompt must be UTF-8") from error
        if request.context_manifest.component_revision != component_revision:
            raise ValueError("bounded context targets another Component revision")
        return cls(
            component_revision,
            request.component_generation_plan_identity,
            request.generation_key_identity,
            request.context_manifest_identity,
            request.complexity_decision_identity,
            request.prompt_identity,
            generation_recipe_identity,
            workspace_allocation_identity,
            workspace_reference,
            prompt,
        )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "component_generation_plan_identity": (
                self.component_generation_plan_identity.to_dict()
            ),
            "generation_key_identity": self.generation_key_identity.to_dict(),
            "context_manifest_identity": self.context_manifest_identity.to_dict(),
            "complexity_decision_identity": (
                self.complexity_decision_identity.to_dict()
            ),
            "prompt_identity": self.prompt_identity.to_dict(),
            "generation_recipe_identity": self.generation_recipe_identity.to_dict(),
            "workspace_allocation_identity": (
                self.workspace_allocation_identity.to_dict()
            ),
            "workspace_reference": self.workspace_reference,
        }


@dataclass(frozen=True, slots=True)
class SourceEvidenceReadiness:
    component_revisions: tuple[ContentIdentity, ...]
    source_snapshot_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    source_ready: bool
    evidence_ready: bool
    missing: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.component_revisions:
            raise ValueError("source readiness requires a Component revision closure")
        for label, values in (
            ("source snapshot IDs", self.source_snapshot_ids),
            ("evidence IDs", self.evidence_ids),
            ("missing readiness items", self.missing),
        ):
            if len(set(values)) != len(values) or any(not item for item in values):
                raise ValueError(f"{label} must be unique non-empty strings")
        if not isinstance(self.source_ready, bool) or not isinstance(
            self.evidence_ready, bool
        ):
            raise TypeError("readiness flags must be booleans")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_revisions": [
                item.to_dict() for item in self.component_revisions
            ],
            "source_snapshot_ids": list(self.source_snapshot_ids),
            "evidence_ids": list(self.evidence_ids),
            "source_ready": self.source_ready,
            "evidence_ready": self.evidence_ready,
            "missing": list(self.missing),
        }


@dataclass(frozen=True, slots=True)
class ComponentSourceEvidenceReadiness:
    """Selected-node readiness; distinct from the legacy full-closure contract."""

    component_revision: ContentIdentity
    source_snapshot_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    source_ready: bool
    evidence_ready: bool
    missing: tuple[str, ...] = ()

    SCHEMA = "urn:literate-ai:schema:v2:component-source-evidence-readiness"

    def __post_init__(self) -> None:
        if not isinstance(self.component_revision, ContentIdentity):
            raise TypeError("Component readiness requires an exact revision")
        for label, values in (
            ("source snapshot IDs", self.source_snapshot_ids),
            ("evidence IDs", self.evidence_ids),
            ("missing readiness items", self.missing),
        ):
            if len(set(values)) != len(values) or any(not item for item in values):
                raise ValueError(f"{label} must be unique non-empty strings")
        if not isinstance(self.source_ready, bool) or not isinstance(
            self.evidence_ready, bool
        ):
            raise TypeError("readiness flags must be booleans")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "source_snapshot_ids": list(self.source_snapshot_ids),
            "evidence_ids": list(self.evidence_ids),
            "source_ready": self.source_ready,
            "evidence_ready": self.evidence_ready,
            "missing": list(self.missing),
        }


@dataclass(frozen=True, slots=True)
class GenerationEvent:
    event_id: str
    sequence: int
    event_type: str
    stage_id: str | None
    data: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.event_id or self.sequence < 1 or not self.event_type:
            raise ValueError("generation event identity and sequence are invalid")
        canonical_json_bytes(dict(self.data))

    @classmethod
    def create(
        cls,
        run_id: str,
        sequence: int,
        event_type: str,
        stage_id: str | None,
        data: Mapping[str, object],
    ) -> GenerationEvent:
        safe = json.loads(canonical_json_bytes(dict(data)))
        identity = canonical_identity(
            {
                "run_id": run_id,
                "sequence": sequence,
                "event_type": event_type,
                "stage_id": stage_id,
                "data": safe,
            }
        )
        return cls(
            f"event:{identity.digest[:24]}", sequence, event_type, stage_id, safe
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "stage_id": self.stage_id,
            "data": dict(self.data),
        }


@dataclass(frozen=True, slots=True)
class StageExecution:
    stage_id: str
    input_identity: ContentIdentity
    output_identity: ContentIdentity
    route_decision: ModelRouteDecision
    response_json: bytes

    def __post_init__(self) -> None:
        if not self.stage_id:
            raise ValueError("model stage execution ID cannot be empty")
        if not isinstance(self.route_decision, ModelRouteDecision):
            raise TypeError("model stage execution requires a typed route decision")
        response = _canonical_mapping_bytes(self.response_json, "model stage response")
        if canonical_identity(response) != self.output_identity:
            raise ValueError("model stage response identity does not match its bytes")

    @property
    def response(self) -> dict[str, object]:
        return json.loads(self.response_json)


@dataclass(frozen=True, slots=True)
class LifecycleExecution:
    step_id: str
    input_identity: ContentIdentity
    output_identity: ContentIdentity
    output_json: bytes

    def __post_init__(self) -> None:
        if not self.step_id:
            raise ValueError("lifecycle execution ID cannot be empty")
        output = _canonical_mapping_bytes(self.output_json, "lifecycle output")
        if canonical_identity(output) != self.output_identity:
            raise ValueError("lifecycle output identity does not match its bytes")

    @property
    def output(self) -> dict[str, object]:
        return json.loads(self.output_json)


@dataclass(frozen=True, slots=True)
class GenerationProvenance:
    run_input_identity: ContentIdentity
    component_lock_identity: ContentIdentity
    root_revision_identity: ContentIdentity
    generated_component_revision_identity: ContentIdentity
    component_generation_plan_identity: ContentIdentity
    generation_key_identity: ContentIdentity
    context_manifest_identity: ContentIdentity
    prompt_identity: ContentIdentity
    generation_recipe_identity: ContentIdentity
    workspace_allocation_identity: ContentIdentity
    build_request_declaration_identity: ContentIdentity
    realized_build_request_identity: ContentIdentity
    readiness_identity: ContentIdentity
    route_decision_digests: tuple[str, ...]
    model_stage_outputs: tuple[ContentIdentity, ...]
    lifecycle_outputs: tuple[ContentIdentity, ...]
    accepted_tree_identity: ContentIdentity

    SCHEMA = "urn:literate-ai:schema:v4:generation-provenance"

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "run_input_identity": self.run_input_identity.to_dict(),
            "component_lock_identity": self.component_lock_identity.to_dict(),
            "root_revision_identity": self.root_revision_identity.to_dict(),
            "generated_component_revision_identity": (
                self.generated_component_revision_identity.to_dict()
            ),
            "component_generation_plan_identity": (
                self.component_generation_plan_identity.to_dict()
            ),
            "generation_key_identity": self.generation_key_identity.to_dict(),
            "context_manifest_identity": self.context_manifest_identity.to_dict(),
            "prompt_identity": self.prompt_identity.to_dict(),
            "generation_recipe_identity": self.generation_recipe_identity.to_dict(),
            "workspace_allocation_identity": (
                self.workspace_allocation_identity.to_dict()
            ),
            "build_request_declaration_identity": (
                self.build_request_declaration_identity.to_dict()
            ),
            "realized_build_request_identity": (
                self.realized_build_request_identity.to_dict()
            ),
            "readiness_identity": self.readiness_identity.to_dict(),
            "route_decision_digests": list(self.route_decision_digests),
            "model_stage_outputs": [
                item.to_dict() for item in self.model_stage_outputs
            ],
            "lifecycle_outputs": [item.to_dict() for item in self.lifecycle_outputs],
            "accepted_tree_identity": self.accepted_tree_identity.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class GenerationRun:
    run_id: str
    input_identity: ContentIdentity
    status: GenerationStatus
    locked_authority: LockedGenerationAuthority
    readiness: ComponentSourceEvidenceReadiness
    route_decisions: tuple[ModelRouteDecision, ...]
    realized_build_request: BuildRequest | None = None
    stage_executions: tuple[StageExecution, ...] = ()
    lifecycle_executions: tuple[LifecycleExecution, ...] = ()
    events: tuple[GenerationEvent, ...] = ()
    provenance: GenerationProvenance | None = None

    def __post_init__(self) -> None:
        if self.run_id != f"generation:{self.input_identity.digest[:24]}":
            raise ValueError("generation run ID does not bind its input identity")
        if not isinstance(self.status, GenerationStatus):
            raise TypeError("generation run status must be typed")
        if not isinstance(self.locked_authority, LockedGenerationAuthority):
            raise TypeError("generation run requires exact locked authority")
        if self.realized_build_request is not None and not isinstance(
            self.realized_build_request, BuildRequest
        ):
            raise TypeError("generation run realized build request must be typed")
        route_ids = tuple(item.policy_id for item in self.route_decisions)
        if len(set(route_ids)) != len(route_ids):
            raise ValueError("generation run route decisions must be unique")
        stage_ids = tuple(item.stage_id for item in self.stage_executions)
        if len(set(stage_ids)) != len(stage_ids):
            raise ValueError("generation run model executions must be unique")
        step_ids = tuple(item.step_id for item in self.lifecycle_executions)
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("generation run lifecycle executions must be unique")
        if tuple(item.sequence for item in self.events) != tuple(
            range(1, len(self.events) + 1)
        ):
            raise ValueError("generation run event sequence is not contiguous")
        for event in self.events:
            if event != GenerationEvent.create(
                self.run_id,
                event.sequence,
                event.event_type,
                event.stage_id,
                event.data,
            ):
                raise ValueError("generation run contains a fabricated event")

    def stage(self, stage_id: str) -> StageExecution | None:
        return next(
            (item for item in self.stage_executions if item.stage_id == stage_id), None
        )

    def step(self, step_id: str) -> LifecycleExecution | None:
        return next(
            (item for item in self.lifecycle_executions if item.step_id == step_id),
            None,
        )

    @property
    def component_lock(self):
        """Exact immutable graph used by this run."""

        return self.locked_authority.lock

    def with_stage(self, execution: StageExecution) -> GenerationRun:
        return replace(self, stage_executions=self.stage_executions + (execution,))

    def with_step(self, execution: LifecycleExecution) -> GenerationRun:
        return replace(
            self, lifecycle_executions=self.lifecycle_executions + (execution,)
        )


def route_decision_dict(decision: ModelRouteDecision) -> dict[str, object]:
    value = asdict(decision)
    value["data_egress"] = decision.data_egress.value
    return value


def _canonical_mapping_bytes(encoded: bytes, label: str) -> dict[str, object]:
    if type(encoded) is not bytes:
        raise TypeError(f"{label} must be canonical JSON bytes")
    try:
        value = json.loads(encoded)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    try:
        canonical = canonical_json_bytes(value)
    except ValueError as exc:
        raise ValueError(f"{label} is outside canonical JSON") from exc
    if canonical != encoded:
        raise ValueError(f"{label} bytes are not canonical JSON")
    return value


__all__ = [
    "FULL_GENERATION_LIFECYCLE_STEPS",
    "ComponentSourceEvidenceReadiness",
    "GenerationEvent",
    "GenerationExecutionPlan",
    "GenerationContextBinding",
    "GenerationProvenance",
    "GenerationRequest",
    "GenerationRun",
    "GenerationStatus",
    "GeneratedTestSuitePolicy",
    "IndependentAcceptancePolicy",
    "LifecycleExecution",
    "ModelStage",
    "SourceEvidenceReadiness",
    "StageExecution",
    "route_decision_dict",
]
