"""VFI-scale proof that sibling Components do not flatten into prompts."""

from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace

from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.application.component_generation_context import (
    PreparedComponentGenerationRequest,
    PromptSegmentInput,
    bind_component_generation_context_cache_key,
    prepare_component_generation_context,
)
from literate_ai.contracts import (
    SourceCacheModelBinding,
    SourceDerivationCacheKey,
    canonical_identity,
)
from literate_ai.contracts.executable_components.context import (
    ContextAuthorityKind,
    GenerationComplexityBudget,
)
from literate_ai.contracts.executable_components.planning import ComponentGenerationPlan
from literate_ai.contracts.identity import ContentIdentity
from tests.support.fixtures_test_component_execution_planning import _models
from tests.support.vfi_scaling import (
    FIXED_FEATURE,
    vfi_component_lock,
)


def _identity(content: bytes) -> ContentIdentity:
    return ContentIdentity.parse_uri(f"sha256:{hashlib.sha256(content).hexdigest()}")


def _fixed_request(lock: object, *, interface_padding: int = 0):
    execution = plan_component_execution(lock, model_identities=_models(lock))
    names = {
        item.revision.identity.uri: item.revision.coordinate.name for item in lock.nodes
    }
    plan = next(
        item
        for item in execution.generation_plans
        if names[item.component_revision.uri] == FIXED_FEATURE
    )
    plan, segments = _materialize(plan, interface_padding=interface_padding)
    return prepare_component_generation_context(
        plan,
        framework_envelope=b"bounded VFI envelope\n",
        authority_segments=segments,
        budget=GenerationComplexityBudget(
            max_prompt_bytes=1_000_000,
            max_estimated_tokens=250_000,
            max_document_count=100,
            max_direct_interface_bytes=100_000,
            max_dependency_fan_in=20,
            max_model_attempts=3,
            max_wall_time_ms=600_000,
            max_model_tokens=100_000,
            max_cost_microunits=50_000_000,
        ),
    )


def _cache_key(
    prepared: PreparedComponentGenerationRequest,
) -> SourceDerivationCacheKey:
    base = SourceDerivationCacheKey(
        recipe_identity=canonical_identity({"fixture": "vfi-recipe"}),
        execution_plan_identity=canonical_identity({"fixture": "vfi-execution"}),
        coding_cli_tool_binding_identity=canonical_identity({"fixture": "coding-cli"}),
        model_binding=SourceCacheModelBinding("test-provider", "test-model"),
        request_identity=prepared.request.prompt_identity,
    )
    return bind_component_generation_context_cache_key(base, prepared)


def _materialize(
    plan: ComponentGenerationPlan, *, interface_padding: int
) -> tuple[ComponentGenerationPlan, tuple[PromptSegmentInput, ...]]:
    key = plan.generation_key
    groups = (
        (ContextAuthorityKind.LOCAL_SPECIFICATION, key.specification_identities),
        (ContextAuthorityKind.LOCAL_FLAVOR, key.flavor_identities),
        (ContextAuthorityKind.LOCAL_SKILL, key.skill_identities),
        (ContextAuthorityKind.LOCAL_WORKFLOW, (key.workflow_identity,)),
        (ContextAuthorityKind.LOCAL_ROUTING_POLICY, (key.routing_identity,)),
        (
            ContextAuthorityKind.LOCAL_PUBLIC_INTERFACE,
            key.exported_public_interface_identities,
        ),
        (
            ContextAuthorityKind.DIRECT_PUBLIC_INTERFACE,
            key.direct_public_interface_identities,
        ),
    )
    replacements: dict[str, ContentIdentity] = {}
    contents: dict[tuple[ContextAuthorityKind, str], bytes] = {}
    for kind, identities in groups:
        for index, old_identity in enumerate(identities):
            padding = (
                b"I" * interface_padding
                if kind is ContextAuthorityKind.DIRECT_PUBLIC_INTERFACE
                else b""
            )
            content = f"{kind.value}:{index}:{old_identity.uri}".encode() + padding
            new_identity = _identity(content)
            replacements[old_identity.uri] = new_identity
            contents[(kind, new_identity.uri)] = content
    updated_key = replace(
        key,
        specification_identities=tuple(
            replacements[item.uri] for item in key.specification_identities
        ),
        flavor_identities=tuple(
            replacements[item.uri] for item in key.flavor_identities
        ),
        skill_identities=tuple(replacements[item.uri] for item in key.skill_identities),
        workflow_identity=replacements[key.workflow_identity.uri],
        routing_identity=replacements[key.routing_identity.uri],
        exported_public_interface_identities=tuple(
            replacements[item.uri] for item in key.exported_public_interface_identities
        ),
        direct_public_interface_identities=tuple(
            replacements[item.uri] for item in key.direct_public_interface_identities
        ),
    )
    edges = tuple(
        replace(
            edge,
            public_interface_identity=replacements[edge.public_interface_identity.uri],
        )
        for edge in plan.direct_generation_edges
    )
    updated = replace(plan, generation_key=updated_key, direct_generation_edges=edges)
    providers = {
        edge.public_interface_identity.uri: edge.provider_revision
        for edge in edges
        if edge.public_interface_identity is not None
    }
    segments = tuple(
        PromptSegmentInput(
            kind,
            (
                providers[item.uri]
                if kind is ContextAuthorityKind.DIRECT_PUBLIC_INTERFACE
                else updated.component_revision
            ),
            f"selected {kind.value}",
            item,
            contents[(kind, item.uri)],
        )
        for kind, identities in (
            (
                ContextAuthorityKind.LOCAL_SPECIFICATION,
                updated_key.specification_identities,
            ),
            (ContextAuthorityKind.LOCAL_FLAVOR, updated_key.flavor_identities),
            (ContextAuthorityKind.LOCAL_SKILL, updated_key.skill_identities),
            (ContextAuthorityKind.LOCAL_WORKFLOW, (updated_key.workflow_identity,)),
            (
                ContextAuthorityKind.LOCAL_ROUTING_POLICY,
                (updated_key.routing_identity,),
            ),
            (
                ContextAuthorityKind.LOCAL_PUBLIC_INTERFACE,
                updated_key.exported_public_interface_identities,
            ),
            (
                ContextAuthorityKind.DIRECT_PUBLIC_INTERFACE,
                updated_key.direct_public_interface_identities,
            ),
        )
        for item in identities
    )
    return updated, segments


class VfiContextScalingTests(unittest.TestCase):
    def test_unrelated_sibling_change_leaves_prompt_and_cache_key_invariant(
        self,
    ) -> None:
        # Each request builds the 50-sibling VFI lock once.
        baseline = _fixed_request(vfi_component_lock())
        unrelated_change = _fixed_request(
            vfi_component_lock(
                specification_overrides={"feature-049": "feature-049-rewritten"},
                interface_overrides={"feature-049": "feature-049-public-v2"},
            )
        )
        local_change = _fixed_request(
            vfi_component_lock(
                specification_overrides={FIXED_FEATURE: "fixed-feature-v2"}
            )
        )
        self.assertEqual(baseline.prompt, unrelated_change.prompt)
        self.assertEqual(
            baseline.request.context_manifest,
            unrelated_change.request.context_manifest,
        )
        self.assertEqual(baseline.request.budget_decision.dependency_fan_in, 1)
        key = _cache_key(baseline)
        self.assertEqual(key, _cache_key(unrelated_change))
        self.assertNotEqual(key, _cache_key(local_change))


if __name__ == "__main__":
    unittest.main()
