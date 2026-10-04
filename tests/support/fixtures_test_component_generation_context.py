from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_component_generation_context``."""

import hashlib
from dataclasses import replace

from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.application.component_generation_context import (
    PromptSegmentInput,
)
from literate_ai.contracts.executable_components.context import (
    ContextAuthorityKind,
    GenerationComplexityBudget,
)
from literate_ai.contracts.executable_components.planning import ComponentGenerationPlan
from literate_ai.contracts.identity import ContentIdentity
from tests.support.fixtures_test_component_execution_planning import _models


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


from tests.support.fixtures_test_component_execution_planning import (
    _diamond_lock,  # noqa: F401
)
# NOTE: names not defined at top level of tests.unit.test_component_generation_context: ['_diamond_lock']
