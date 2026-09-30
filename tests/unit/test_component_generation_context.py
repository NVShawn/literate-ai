"""Adversarial bounded-context tests for Component generation."""

from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace

from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.application.component_generation_context import (
    ComponentGenerationContextError,
    GenerationComplexityBudgetExceeded,
    PromptSegmentInput,
    prepare_component_generation_context,
)
from literate_ai.contracts.executable_components.context import (
    BoundedComponentGenerationRequest,
    ComponentGenerationContextManifest,
    ContextAuthorityKind,
    ContextVisibility,
    GenerationComplexityBudget,
)
from literate_ai.contracts.executable_components.planning import ComponentGenerationPlan
from literate_ai.contracts.identity import ContentIdentity
from tests.unit.test_component_execution_planning import _diamond_lock, _models


def _identity(content: bytes) -> ContentIdentity:
    return ContentIdentity.parse_uri(f"sha256:{hashlib.sha256(content).hexdigest()}")


def _budget(**changes: int) -> GenerationComplexityBudget:
    values = {
        "max_prompt_bytes": 1_000_000,
        "max_estimated_tokens": 250_000,
        "max_document_count": 100,
        "max_direct_interface_bytes": 100_000,
        "max_dependency_fan_in": 20,
        "max_model_attempts": 3,
        "max_wall_time_ms": 600_000,
        "max_model_tokens": 100_000,
        "max_cost_microunits": 50_000_000,
    }
    values.update(changes)
    return GenerationComplexityBudget(**values)


def _named_plan(lock: object, name: str) -> ComponentGenerationPlan:
    execution = plan_component_execution(lock, model_identities=_models(lock))
    names = {
        item.revision.identity.uri: item.revision.coordinate.name for item in lock.nodes
    }
    return next(
        item
        for item in execution.generation_plans
        if names[item.component_revision.uri] == name
    )


def _materialize(
    plan: ComponentGenerationPlan,
    *,
    suffix: str = "",
) -> tuple[ComponentGenerationPlan, tuple[PromptSegmentInput, ...]]:
    """Give synthetic planned references exact test bytes and matching identities."""

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
        (ContextAuthorityKind.LOCAL_ASSET_METADATA, key.asset_identities),
        (
            ContextAuthorityKind.DIRECT_PUBLIC_INTERFACE,
            key.direct_public_interface_identities,
        ),
    )
    replacements: dict[str, ContentIdentity] = {}
    contents: dict[tuple[ContextAuthorityKind, str], bytes] = {}
    for kind, identities in groups:
        for index, old_identity in enumerate(identities):
            content = f"{kind.value}:{index}:{old_identity.uri}{suffix}".encode()
            new_identity = _identity(content)
            replacements[old_identity.uri] = new_identity
            contents[(kind, new_identity.uri)] = content

    updated_edges = tuple(
        replace(
            edge,
            public_interface_identity=(
                replacements[edge.public_interface_identity.uri]
                if edge.public_interface_identity is not None
                else None
            ),
        )
        for edge in plan.direct_generation_edges
    )
    updated_key = replace(
        key,
        specification_identities=tuple(
            replacements[item.uri] for item in key.specification_identities
        ),
        flavor_identities=tuple(
            sorted(
                (replacements[item.uri] for item in key.flavor_identities),
                key=lambda item: item.uri,
            )
        ),
        skill_identities=tuple(
            sorted(
                (replacements[item.uri] for item in key.skill_identities),
                key=lambda item: item.uri,
            )
        ),
        workflow_identity=replacements[key.workflow_identity.uri],
        routing_identity=replacements[key.routing_identity.uri],
        exported_public_interface_identities=tuple(
            sorted(
                (
                    replacements[item.uri]
                    for item in key.exported_public_interface_identities
                ),
                key=lambda item: item.uri,
            )
        ),
        asset_identities=tuple(
            sorted(
                (replacements[item.uri] for item in key.asset_identities),
                key=lambda item: item.uri,
            )
        ),
        direct_public_interface_identities=tuple(
            sorted(
                (
                    replacements[item.uri]
                    for item in key.direct_public_interface_identities
                ),
                key=lambda item: item.uri,
            )
        ),
    )
    updated = replace(
        plan, generation_key=updated_key, direct_generation_edges=updated_edges
    )
    providers = {
        edge.public_interface_identity.uri: edge.provider_revision
        for edge in updated.direct_generation_edges
        if edge.public_interface_identity is not None
    }
    inputs = tuple(
        PromptSegmentInput(
            kind,
            (
                providers[identity.uri]
                if kind is ContextAuthorityKind.DIRECT_PUBLIC_INTERFACE
                else updated.component_revision
            ),
            f"selected {kind.value}",
            identity,
            contents[(kind, identity.uri)],
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
            (ContextAuthorityKind.LOCAL_ASSET_METADATA, updated_key.asset_identities),
            (
                ContextAuthorityKind.DIRECT_PUBLIC_INTERFACE,
                updated_key.direct_public_interface_identities,
            ),
        )
        for identity in identities
    )
    return updated, inputs


class ComponentGenerationContextTests(unittest.TestCase):
    def test_locked_local_asset_metadata_is_admitted_as_local_authority(self) -> None:
        original = _named_plan(_diamond_lock(), "invoice-cli")
        plan, inputs = _materialize(
            replace(
                original,
                generation_key=replace(
                    original.generation_key,
                    asset_identities=(_identity(b"locked asset metadata"),),
                ),
            )
        )

        prepared = prepare_component_generation_context(
            plan,
            framework_envelope=b"trusted envelope",
            authority_segments=inputs,
            budget=_budget(),
        )

        assets = tuple(
            item
            for item in prepared.request.context_manifest.segments
            if item.authority_kind is ContextAuthorityKind.LOCAL_ASSET_METADATA
        )
        self.assertEqual(len(assets), 1)
        self.assertIs(assets[0].visibility, ContextVisibility.LOCAL_AUTHORITY)
        self.assertEqual(assets[0].source_component_revision, plan.component_revision)

    def test_diamond_manifest_is_deterministic_and_identity_bound(self) -> None:
        plan, inputs = _materialize(_named_plan(_diamond_lock(), "invoice-cli"))
        first = prepare_component_generation_context(
            plan,
            framework_envelope=b"trusted envelope",
            authority_segments=tuple(reversed(inputs)),
            budget=_budget(),
        )
        second = prepare_component_generation_context(
            plan,
            framework_envelope=b"trusted envelope",
            authority_segments=inputs,
            budget=_budget(),
        )
        self.assertEqual(first, second)
        self.assertEqual(
            first.request.context_manifest,
            ComponentGenerationContextManifest.from_dict(
                first.request.context_manifest.to_dict()
            ),
        )
        self.assertEqual(
            first.request,
            BoundedComponentGenerationRequest.from_dict(first.request.to_dict()),
        )
        self.assertEqual(len(first.prompt), first.request.budget_decision.prompt_bytes)
        self.assertEqual(
            first.request.component_generation_plan_identity, plan.identity
        )
        self.assertEqual(
            first.request.generation_key_identity, plan.generation_key.identity
        )
        interfaces = [
            item
            for item in first.request.context_manifest.segments
            if item.authority_kind is ContextAuthorityKind.DIRECT_PUBLIC_INTERFACE
        ]
        self.assertEqual(len(interfaces), 2)
        self.assertEqual(
            {item.source_component_revision.uri for item in interfaces},
            {edge.provider_revision.uri for edge in plan.direct_generation_edges},
        )

    def test_private_transitive_canaries_are_rejected_even_when_names_collide(
        self,
    ) -> None:
        plan, inputs = _materialize(_named_plan(_diamond_lock(), "invoice-cli"))
        public = next(
            item
            for item in inputs
            if item.authority_kind is ContextAuthorityKind.DIRECT_PUBLIC_INTERFACE
        )
        for forbidden_kind in (
            ContextAuthorityKind.PRIVATE_DEPENDENCY_SPECIFICATION,
            ContextAuthorityKind.DEPENDENCY_SOURCE,
            ContextAuthorityKind.DEPENDENCY_TEST,
            ContextAuthorityKind.DEPENDENCY_MANIFEST,
            ContextAuthorityKind.DEPENDENCY_LOCK,
            ContextAuthorityKind.INVERSE_JOURNAL,
            ContextAuthorityKind.ACCEPTANCE_ORACLE,
            ContextAuthorityKind.TRANSITIVE_PUBLIC_INTERFACE,
        ):
            canary = PromptSegmentInput(
                forbidden_kind,
                public.source_component_revision,
                "interface-name-collision: READ_PRIVATE_DEPENDENCY_NOW",
                public.content_identity,
                public.content,
            )
            with (
                self.subTest(kind=forbidden_kind.value),
                self.assertRaises(ComponentGenerationContextError) as error,
            ):
                prepare_component_generation_context(
                    plan,
                    framework_envelope=b"trusted envelope",
                    authority_segments=(*inputs, canary),
                    budget=_budget(),
                )
            self.assertEqual(
                error.exception.code, "generation_context.authority_forbidden"
            )

    def test_chain_depth_and_private_leaf_change_have_zero_consumer_effect(
        self,
    ) -> None:
        baseline, baseline_inputs = _materialize(
            _named_plan(_diamond_lock(), "invoice-cli")
        )
        changed, changed_inputs = _materialize(
            _named_plan(
                _diamond_lock(money_spec="deep-private-canary-READ-ME"),
                "invoice-cli",
            )
        )
        left = prepare_component_generation_context(
            baseline,
            framework_envelope=b"trusted envelope",
            authority_segments=baseline_inputs,
            budget=_budget(),
        )
        right = prepare_component_generation_context(
            changed,
            framework_envelope=b"trusted envelope",
            authority_segments=changed_inputs,
            budget=_budget(),
        )
        self.assertEqual(
            left.request.context_manifest.identity,
            right.request.context_manifest.identity,
        )
        self.assertEqual(
            left.request.generation_key_identity, right.request.generation_key_identity
        )
        self.assertNotIn(b"deep-private-canary", right.prompt)

    def test_exported_interface_changes_only_its_direct_consumers(self) -> None:
        baseline, baseline_inputs = _materialize(
            _named_plan(_diamond_lock(), "pricing")
        )
        changed, changed_inputs = _materialize(
            _named_plan(
                _diamond_lock(money_interface="money-interface-public-v2"), "pricing"
            )
        )
        left = prepare_component_generation_context(
            baseline,
            framework_envelope=b"trusted envelope",
            authority_segments=baseline_inputs,
            budget=_budget(),
        )
        right = prepare_component_generation_context(
            changed,
            framework_envelope=b"trusted envelope",
            authority_segments=changed_inputs,
            budget=_budget(),
        )
        self.assertNotEqual(
            left.request.context_manifest.identity,
            right.request.context_manifest.identity,
        )
        self.assertNotEqual(
            left.request.generation_key_identity, right.request.generation_key_identity
        )

    def test_wide_fan_in_grows_only_by_interfaces_and_fails_before_egress(self) -> None:
        plan, inputs = _materialize(_named_plan(_diamond_lock(), "invoice-cli"))
        with self.assertRaises(GenerationComplexityBudgetExceeded) as error:
            prepare_component_generation_context(
                plan,
                framework_envelope=b"trusted envelope",
                authority_segments=inputs,
                budget=_budget(max_dependency_fan_in=1),
            )
        decision = error.exception.decision
        self.assertEqual(decision.violations, ("dependency_fan_in",))
        self.assertEqual(decision.dependency_fan_in, 2)
        self.assertEqual(len(decision.largest_interface_contributors), 2)
        self.assertIn("refactor the Component", str(error.exception))

    def test_byte_token_document_and_interface_limits_report_largest_inputs(
        self,
    ) -> None:
        plan, inputs = _materialize(_named_plan(_diamond_lock(), "invoice-cli"))
        with self.assertRaises(GenerationComplexityBudgetExceeded) as error:
            prepare_component_generation_context(
                plan,
                framework_envelope=b"trusted envelope",
                authority_segments=inputs,
                budget=_budget(
                    max_prompt_bytes=1,
                    max_estimated_tokens=1,
                    max_document_count=1,
                    max_direct_interface_bytes=1,
                ),
            )
        self.assertEqual(
            error.exception.decision.violations,
            (
                "direct_interface_bytes",
                "document_count",
                "estimated_tokens",
                "prompt_bytes",
            ),
        )
        self.assertTrue(error.exception.decision.largest_local_contributors)
        self.assertTrue(error.exception.decision.largest_interface_contributors)

    def test_drift_wrong_source_omission_and_extra_transitive_interface_fail_closed(
        self,
    ) -> None:
        plan, inputs = _materialize(_named_plan(_diamond_lock(), "invoice-cli"))
        cases = (
            (
                replace(inputs[0], content=inputs[0].content + b" drift"),
                "generation_context.content_drift",
            ),
            (
                replace(
                    inputs[0],
                    source_component_revision=plan.direct_generation_edges[
                        0
                    ].provider_revision,
                ),
                "generation_context.source_component_mismatch",
            ),
        )
        for replacement, code in cases:
            with (
                self.subTest(code=code),
                self.assertRaises(ComponentGenerationContextError) as error,
            ):
                prepare_component_generation_context(
                    plan,
                    framework_envelope=b"trusted envelope",
                    authority_segments=(replacement, *inputs[1:]),
                    budget=_budget(),
                )
            self.assertEqual(error.exception.code, code)

        with self.assertRaises(ComponentGenerationContextError) as omission:
            prepare_component_generation_context(
                plan,
                framework_envelope=b"trusted envelope",
                authority_segments=inputs[:-1],
                budget=_budget(),
            )
        self.assertEqual(
            omission.exception.code, "generation_context.authority_incomplete"
        )

        transitive_content = b"private transitive public-looking interface"
        transitive = PromptSegmentInput(
            ContextAuthorityKind.DIRECT_PUBLIC_INTERFACE,
            plan.direct_generation_edges[0].provider_revision,
            "not actually direct",
            _identity(transitive_content),
            transitive_content,
        )
        with self.assertRaises(ComponentGenerationContextError) as extra:
            prepare_component_generation_context(
                plan,
                framework_envelope=b"trusted envelope",
                authority_segments=(*inputs, transitive),
                budget=_budget(),
            )
        self.assertEqual(
            extra.exception.code, "generation_context.authority_unselected"
        )


if __name__ == "__main__":
    unittest.main()
