from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_component_lock_contracts``."""


from dataclasses import replace

from literate_ai.contracts.capabilities import (
    CapabilityRequirement,
    DependencyKind,
)
from literate_ai.contracts.component_locking import (
    AuthoredProvidedCapability,
    ComponentAuthoring,
    ComponentContentSelector,
    ComponentLock,
    ComponentLockNode,
    LockedComponentRevision,
    RequirementConstraintSatisfaction,
    ordered_specification_set_identity,
)
from literate_ai.contracts.components import Entrypoint
from literate_ai.contracts.executable_components import (
    ComponentInterfaceBinding,
    ExecutableComponentEdge,
    NodeFlavorSlotResolution,
    NodeTargetFlavorSelection,
    SelectedNodeFlavor,
)
from literate_ai.contracts.flavors import (
    FlavorAxis,
    FlavorCardinality,
    FlavorSlot,
)
from literate_ai.contracts.identity import (
    ComponentCoordinate,
    ContentIdentity,
    ContentReference,
    canonical_identity,
)


def identity(label: str) -> ContentIdentity:
    return canonical_identity({"fixture": label})


def reference(kind: str, uri: str, label: str | None = None) -> ContentReference:
    return ContentReference(kind, uri, identity(label or uri))


def selector(
    kind: str,
    uri: str,
    *,
    pin: ContentIdentity | None = None,
) -> ComponentContentSelector:
    return ComponentContentSelector(kind, uri, pin)


def component_authoring(
    name: str,
    *,
    interface: tuple[str, str] | None = None,
    requirements: tuple[CapabilityRequirement, ...] = (),
) -> ComponentAuthoring:
    provides = (
        ()
        if interface is None
        else (
            AuthoredProvidedCapability(
                interface[0],
                "1.0.0",
                selector(
                    "public-interface-contract",
                    f"interfaces/{interface[0]}.json",
                ),
            ),
        )
    )
    return ComponentAuthoring(
        coordinate=ComponentCoordinate("samples", name),
        version="1.0.0",
        display_name=name,
        description="Lock contract fixture",
        profiles=("portable",),
        sample=True,
        provides=provides,
        requires=requirements,
        specification_provider="literate-markdown",
        specification_roots=(f"specs/{name}.md",),
        authoring_inputs=(
            selector("specification-to-source-skill", "skills/implement.md"),
            selector("specification-to-source-skill", "skills/plan.md"),
        ),
        workflow_definition=selector("workflow", "workflows/host.json"),
        routing_policy=selector("routing-policy", "routing/default.json"),
        flavor_slots=(
            FlavorSlot(
                "language",
                FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
                FlavorCardinality.EXACTLY_ONE,
                "portable-application",
            ),
        ),
        entrypoints=(Entrypoint("run", "portable-application", "run"),),
        acceptance_contracts=(
            selector("acceptance-contract", "acceptance/execution.json"),
        ),
    )


def locked_revision(
    name: str,
    *,
    selected_flavor: str,
    interface: tuple[str, str] | None = None,
    authoring: ComponentAuthoring | None = None,
) -> LockedComponentRevision:
    selected_authoring = authoring or component_authoring(name, interface=interface)
    interfaces = (
        ()
        if interface is None
        else (
            reference(
                "public-interface-contract",
                f"interfaces/{interface[0]}.json",
                interface[1],
            ),
        )
    )
    specifications = (
        reference("specification", f"specs/{name}.md", f"{name}-specification"),
    )
    return LockedComponentRevision(
        coordinate=selected_authoring.coordinate,
        version=selected_authoring.version,
        authoring_identity=selected_authoring.identity,
        specification_set_identity=ordered_specification_set_identity(
            selected_authoring.specification_provider, specifications
        ),
        specifications=specifications,
        selected_flavor_revisions=(identity(f"flavor-{selected_flavor}"),),
        authoring_inputs=(
            reference(
                "specification-to-source-skill",
                "skills/implement.md",
                "implementation-skill",
            ),
            reference(
                "specification-to-source-skill",
                "skills/plan.md",
                "planning-skill",
            ),
        ),
        workflow_definition=reference("workflow", "workflows/host.json", "workflow"),
        routing_policy=reference("routing-policy", "routing/default.json", "routing"),
        acceptance_contracts=(
            reference("acceptance-contract", "acceptance/execution.json", "acceptance"),
        ),
        repository_sources=(),
        public_interfaces=interfaces,
        definition=selected_authoring,
    )


def node(
    revision: LockedComponentRevision,
    *,
    selected_flavor: str,
    capability: str | None = None,
    target_profile_identity: ContentIdentity | None = None,
) -> ComponentLockNode:
    revision_identity = revision.identity
    selection = NodeTargetFlavorSelection(
        component_revision=revision_identity,
        target_name="host",
        target_profile_identity=(
            identity("invoice-cli-target")
            if target_profile_identity is None
            else target_profile_identity
        ),
        selection_policy_identity=identity("selection-policy"),
        slots=(
            NodeFlavorSlotResolution(
                FlavorSlot(
                    "language",
                    FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
                    FlavorCardinality.EXACTLY_ONE,
                    "portable-application",
                ),
                (
                    SelectedNodeFlavor(
                        selected_flavor,
                        identity(f"flavor-{selected_flavor}"),
                    ),
                ),
            ),
        ),
    )
    bindings = (
        ()
        if capability is None
        else (
            ComponentInterfaceBinding(
                revision_identity,
                capability,
                revision.public_interfaces[0].identity,
            ),
        )
    )
    return ComponentLockNode(revision, selection, bindings)


def component_lock() -> ComponentLock:
    invoice_authoring = component_authoring(
        "invoice-cli",
        requirements=(
            CapabilityRequirement(
                "pricing",
                "pricing-api",
                ">=1,<2",
                DependencyKind.GENERATION,
            ),
        ),
    )
    pricing_authoring = component_authoring(
        "pricing", interface=("pricing-api", "pricing-interface")
    )
    invoice = node(
        locked_revision(
            "invoice-cli",
            selected_flavor="rust",
            authoring=invoice_authoring,
        ),
        selected_flavor="rust",
    )
    pricing = node(
        locked_revision(
            "pricing",
            selected_flavor="python",
            interface=("pricing-api", "pricing-interface"),
            authoring=pricing_authoring,
        ),
        selected_flavor="python",
        capability="pricing-api",
    )
    edge = ExecutableComponentEdge(
        consumer_revision=invoice.revision.identity,
        provider_revision=pricing.revision.identity,
        requirement_id="pricing",
        capability="pricing-api",
        kind=DependencyKind.GENERATION,
        public_interface_identity=pricing.interface_bindings[0].interface_identity,
    )
    invoice = replace(
        invoice,
        requirement_constraint_satisfactions=(
            RequirementConstraintSatisfaction(
                consumer_revision=invoice.revision.identity,
                provider_revision=pricing.revision.identity,
                requirement_id="pricing",
                constraints=invoice_authoring.requires[0].constraints,
                target_name=invoice.target_flavor_selection.target_name,
                target_profile_identity=(
                    invoice.target_flavor_selection.target_profile_identity
                ),
                selection_policy_identity=(
                    invoice.target_flavor_selection.selection_policy_identity
                ),
                target_flavor_selection_identity=(
                    invoice.target_flavor_selection.identity
                ),
                satisfaction_evidence_identity=identity(
                    "pricing-constraint-satisfaction"
                ),
            ),
        ),
    )
    nodes = tuple(
        sorted((invoice, pricing), key=lambda item: item.revision.identity.uri)
    )
    return ComponentLock(
        target_name="host",
        target_profile_identity=identity("invoice-cli-target"),
        selection_policy_identity=identity("selection-policy"),
        resolver_identity=identity("component-lock-resolver"),
        root_revision=invoice.revision.identity,
        nodes=nodes,
        edges=(edge,),
        _authorings=tuple(
            sorted(
                (invoice_authoring, pricing_authoring),
                key=lambda item: item.identity.uri,
            )
        ),
    )
