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
    ContractValidationError,
    SourceCacheModelBinding,
    SourceDerivationCacheKey,
    canonical_identity,
)
from literate_ai.contracts.executable_components.context import (
    ContextAuthorityKind,
    ContextVisibility,
    GenerationComplexityBudget,
)
from literate_ai.contracts.executable_components.planning import ComponentGenerationPlan
from literate_ai.contracts.identity import ContentIdentity
from tests.support.vfi_scaling import (
    DIRECT_PROVIDER,
    FIXED_FEATURE,
    PRIVATE_DESCENDANT,
    vfi_component_lock,
)
from tests.unit.test_component_execution_planning import _models


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
    def test_fifty_siblings_are_order_independent_and_do_not_flatten(self) -> None:
        names = tuple(f"feature-{index:03d}" for index in range(50))
        forward = _fixed_request(vfi_component_lock(sibling_order=names))
        reverse = _fixed_request(
            vfi_component_lock(sibling_order=tuple(reversed(names)))
        )
        self.assertEqual(forward, reverse)
        self.assertEqual(forward.request.budget_decision.dependency_fan_in, 1)
        self.assertEqual(
            sum(
                segment.visibility is ContextVisibility.DIRECT_PUBLIC_INTERFACE
                for segment in forward.request.context_manifest.segments
            ),
            1,
        )

    def test_unrelated_sibling_addition_and_change_leave_prompt_invariant(self) -> None:
        baseline = _fixed_request(vfi_component_lock())
        added = _fixed_request(vfi_component_lock(sibling_count=51))
        changed = _fixed_request(
            vfi_component_lock(
                specification_overrides={"feature-049": "feature-049-rewritten"},
                interface_overrides={"feature-049": "feature-049-public-v2"},
            )
        )
        self.assertEqual(baseline.prompt, added.prompt)
        self.assertEqual(baseline.prompt, changed.prompt)
        self.assertEqual(
            baseline.request.context_manifest, added.request.context_manifest
        )
        self.assertEqual(
            baseline.request.context_manifest, changed.request.context_manifest
        )

    def test_private_descendant_change_leaves_prompt_invariant(self) -> None:
        baseline = _fixed_request(vfi_component_lock())
        changed = _fixed_request(
            vfi_component_lock(
                specification_overrides={
                    PRIVATE_DESCENDANT: "private-descendant-canary-READ-ME"
                }
            )
        )
        self.assertEqual(baseline.prompt, changed.prompt)
        self.assertNotIn(b"private-descendant-canary", changed.prompt)
        self.assertEqual(
            baseline.request.context_manifest, changed.request.context_manifest
        )

    def test_only_direct_interface_bytes_enlarge_fixed_component_prompt(self) -> None:
        baseline = _fixed_request(vfi_component_lock())
        growth = 257
        enlarged = _fixed_request(
            vfi_component_lock(
                interface_overrides={DIRECT_PROVIDER: "feature-001-public-v2"}
            ),
            interface_padding=growth,
        )
        left = baseline.request.context_manifest.segments
        right = enlarged.request.context_manifest.segments
        left_local = tuple(
            item
            for item in left
            if item.visibility is ContextVisibility.LOCAL_AUTHORITY
        )
        right_local = tuple(
            item
            for item in right
            if item.visibility is ContextVisibility.LOCAL_AUTHORITY
        )
        self.assertEqual(left_local, right_local)
        self.assertEqual(
            enlarged.request.budget_decision.prompt_bytes
            - baseline.request.budget_decision.prompt_bytes,
            growth,
        )
        self.assertEqual(
            enlarged.request.budget_decision.direct_interface_bytes
            - baseline.request.budget_decision.direct_interface_bytes,
            growth,
        )

    def test_cache_key_tracks_local_and_direct_context_not_graph_neighborhood(
        self,
    ) -> None:
        baseline = _fixed_request(vfi_component_lock())
        unrelated_add = _fixed_request(vfi_component_lock(sibling_count=51))
        unrelated_change = _fixed_request(
            vfi_component_lock(
                specification_overrides={"feature-049": "unrelated-private-v2"},
                interface_overrides={"feature-049": "unrelated-public-v2"},
            )
        )
        private_change = _fixed_request(
            vfi_component_lock(
                specification_overrides={PRIVATE_DESCENDANT: "private-v2"}
            )
        )
        local_change = _fixed_request(
            vfi_component_lock(
                specification_overrides={FIXED_FEATURE: "fixed-feature-v2"}
            )
        )
        interface_change = _fixed_request(
            vfi_component_lock(
                interface_overrides={DIRECT_PROVIDER: "direct-interface-v2"}
            )
        )

        key = _cache_key(baseline)
        self.assertEqual(
            baseline.request.context_manifest_identity,
            baseline.request.context_manifest.identity,
        )
        self.assertEqual(
            baseline.request.complexity_decision_identity,
            baseline.request.budget_decision.identity,
        )
        self.assertEqual(key, SourceDerivationCacheKey.from_dict(key.to_dict()))
        self.assertEqual(key, _cache_key(unrelated_add))
        self.assertEqual(key, _cache_key(unrelated_change))
        self.assertEqual(key, _cache_key(private_change))
        self.assertNotEqual(key, _cache_key(local_change))
        self.assertNotEqual(key, _cache_key(interface_change))

        forged = baseline.request.to_dict()
        forged["context_manifest_identity"] = canonical_identity(
            {"fixture": "forged-context"}
        ).to_dict()
        with self.assertRaisesRegex(ContractValidationError, "explicit context"):
            type(baseline.request).from_dict(forged)


if __name__ == "__main__":
    unittest.main()
