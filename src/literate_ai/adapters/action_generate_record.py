"""Portable GENERATE request custody; private runtime resolution remains separate."""

import json
from dataclasses import dataclass

from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_retained_source import RetainedGenerationInput
from literate_ai.application.component_generation_context import (
    PromptSegmentInput,
    prepare_component_generation_context,
)
from literate_ai.application.source_generation_scheduling import (
    validate_prepared_component_generation_node,
)
from literate_ai.contracts import (
    ComponentExecutionPlan,
    ContentIdentity,
    canonical_json_bytes,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.executable_components.context import (
    BoundedComponentGenerationRequest,
)

GENERATE_INPUT_IDENTITY_ENV = "LITAI_GENERATE_INPUT_IDENTITY"
GENERATE_DEADLINE_ENV = "LITAI_GENERATE_DEADLINE"
GENERATE_CAS_ENV = "LITAI_GENERATE_CAS"
GENERATE_WORKSPACE_ENV = "LITAI_GENERATE_WORKSPACE"

MAX_GENERATE_PROMPT_BYTES = 64 * 1024 * 1024


def _invalid():
    raise ActionWireError(
        "action_generate.input_invalid", "GENERATE input custody refused"
    )


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _invalid()
        result[key] = value
    return result


@dataclass(frozen=True)
class GenerateWorkerInput:
    execution_plan: ComponentExecutionPlan
    generation_plan_identity: ContentIdentity
    request: BoundedComponentGenerationRequest
    recipe_identity: ContentIdentity
    workspace_allocation_identity: ContentIdentity
    prompt: BlobRef
    retained: RetainedGenerationInput | None = None

    @property
    def plan(self):
        matches = tuple(
            plan
            for plan in self.execution_plan.generation_plans
            if plan.identity == self.generation_plan_identity
        )
        if len(matches) != 1:
            _invalid()
        return matches[0]

    def to_bytes(self):
        value = dict(
            schema="literate-ai/generate-worker-input@1",
            execution_plan=self.execution_plan.to_dict(),
            generation_plan_identity=self.generation_plan_identity.uri,
            request=self.request.to_dict(),
            recipe_identity=self.recipe_identity.uri,
            workspace_allocation_identity=self.workspace_allocation_identity.uri,
            prompt=self.prompt.to_dict(),
        )
        if self.retained is not None:
            value["schema"] = "literate-ai/generate-worker-input@2"
            value["retained"] = json.loads(self.retained.to_bytes())
        return canonical_json_bytes(value)

    @classmethod
    def capture(
        cls,
        execution_plan,
        prepared,
        cas,
        deadline,
        *,
        retained=None,
        authorization=None,
    ):
        deadline.remaining()
        validate_prepared_component_generation_node(prepared)
        raw_recipe = prepared.recipe.identity
        recipe = (
            raw_recipe
            if isinstance(raw_recipe, ContentIdentity)
            else ContentIdentity.parse_uri(raw_recipe)
        )
        if len(prepared.request.prompt) > MAX_GENERATE_PROMPT_BYTES:
            _invalid()
        if retained is None and authorization is not None:
            _invalid()
        retained_input = (
            RetainedGenerationInput.capture(retained, authorization, cas, deadline)
            if retained is not None
            else None
        )
        value = cls(
            execution_plan,
            prepared.plan.identity,
            prepared.request.request,
            recipe,
            prepared.workspace.allocation_identity,
            cas.put_bytes(prepared.request.prompt),
            retained_input,
        )
        content = value.to_bytes()
        admitted = cls.admit(content, record_identity(content), deadline)
        admitted.read_prompt(prepared.request.prompt, deadline)
        return admitted

    @classmethod
    def admit(cls, content, identity, deadline):
        deadline.remaining()
        if (
            not isinstance(content, bytes)
            or len(content) > MAX_ACTION_RECORD_BYTES
            or not isinstance(identity, ContentIdentity)
            or record_identity(content) != identity
        ):
            _invalid()
        try:
            doc = json.loads(content, object_pairs_hook=_pairs)
            version_two = (
                isinstance(doc, dict)
                and doc.get("schema") == "literate-ai/generate-worker-input@2"
            )
            if (
                not isinstance(doc, dict)
                or set(doc)
                != (
                    {
                        "schema",
                        "execution_plan",
                        "generation_plan_identity",
                        "request",
                        "recipe_identity",
                        "workspace_allocation_identity",
                        "prompt",
                    }
                    | ({"retained"} if version_two else set())
                )
                or doc["schema"]
                not in {
                    "literate-ai/generate-worker-input@1",
                    "literate-ai/generate-worker-input@2",
                }
                or canonical_json_bytes(doc) != content
            ):
                _invalid()
            retained = None
            if version_two:
                raw_retained = canonical_json_bytes(doc["retained"])
                retained = RetainedGenerationInput.admit(
                    raw_retained, record_identity(raw_retained), deadline
                )
            value = cls(
                ComponentExecutionPlan.from_dict(doc["execution_plan"]),
                ContentIdentity.parse_uri(doc["generation_plan_identity"]),
                BoundedComponentGenerationRequest.from_dict(doc["request"]),
                ContentIdentity.parse_uri(doc["recipe_identity"]),
                ContentIdentity.parse_uri(doc["workspace_allocation_identity"]),
                BlobRef.from_dict(doc["prompt"]),
                retained,
            )
            if (
                retained is not None
                and ContentIdentity.from_dict(
                    json.loads(retained.metadata)["component_lock_identity"]
                )
                != value.execution_plan.component_lock_identity
            ):
                _invalid()
            plan, request = value.plan, value.request
            if (
                request.component_generation_plan_identity != plan.identity
                or request.generation_key_identity != plan.generation_key.identity
                or request.context_manifest.component_revision
                != plan.component_revision
                or value.prompt.identity != request.prompt_identity.uri
                or not 0 < value.prompt.size <= MAX_GENERATE_PROMPT_BYTES
                or value.prompt.size != request.context_manifest.byte_count
                or value.prompt.size != request.budget_decision.prompt_bytes
                or value.prompt.size > request.budget.max_prompt_bytes
            ):
                _invalid()
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
            raise ActionWireError(
                "action_generate.input_invalid", "GENERATE envelope refused"
            ) from exc
        deadline.remaining()
        return value

    def read_prompt(self, content, deadline):
        """Recompute exact authority projection and budget before any model egress."""
        deadline.remaining()
        if (
            not isinstance(content, bytes)
            or len(content) != self.prompt.size
            or len(content) > MAX_GENERATE_PROMPT_BYTES
            or record_identity(content).uri != self.prompt.identity
        ):
            _invalid()
        parts, offset = [], 0
        for segment in self.request.context_manifest.segments:
            part = content[offset : offset + segment.byte_count]
            if record_identity(part) != segment.content_identity:
                _invalid()
            parts.append(part)
            offset += segment.byte_count
        if offset != len(content):
            _invalid()
        try:
            result = prepare_component_generation_context(
                self.plan,
                framework_envelope=parts[0],
                authority_segments=tuple(
                    PromptSegmentInput(
                        segment.authority_kind,
                        segment.source_component_revision,
                        segment.reason,
                        segment.content_identity,
                        part,
                    )
                    for segment, part in zip(
                        self.request.context_manifest.segments[1:],
                        parts[1:],
                        strict=True,
                    )
                ),
                budget=self.request.budget,
            )
            if result.request != self.request or result.prompt != content:
                _invalid()
        except (ValueError, TypeError) as exc:
            raise ActionWireError(
                "action_generate.prompt_invalid", "GENERATE prompt authority refused"
            ) from exc
        deadline.remaining()
        return result
