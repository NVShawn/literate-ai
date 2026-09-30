"""Selected Component lock graph and non-authoritative catalog audit tests."""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.component_acceptance import (
    root_component_name,
    root_entrypoint_kind,
)
from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.capabilities import (
    CapabilityConstraint,
    CapabilityRequirement,
    DependencyKind,
)
from literate_ai.contracts.component_locking import (
    AuthoredProvidedCapability,
    AuthoredRepositorySourceDependency,
    ComponentAuthoring,
    ComponentContentSelector,
    ComponentLock,
    ComponentLockNode,
    ComponentResolutionAudit,
    LockedComponentRevision,
    LockedFlavorRequirement,
    NodeFlavorCandidateAudit,
    RequirementConstraintSatisfaction,
    ordered_specification_set_identity,
)
from literate_ai.contracts.component_locking.locks import (
    _require_closed_acyclic_graph,
    _require_node_authoring,
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
    CandidateStatus,
    FlavorAxis,
    FlavorCardinality,
    FlavorDefinition,
    FlavorRevision,
    FlavorSlot,
)
from literate_ai.contracts.identity import (
    ComponentCoordinate,
    ContentIdentity,
    ContentReference,
    FlavorCoordinate,
    canonical_identity,
)
from literate_ai.contracts.repositories import (
    RepositoryRevisionKind,
    RepositoryRevisionSelector,
    RepositorySourceDependency,
    RepositorySourceLock,
)
from tests.unit.test_component_authoring_lock_contracts import schema_catalog

ROOT = Path(__file__).resolve().parents[2]


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


def locked_flavor_requirement(
    requirement: CapabilityRequirement,
) -> LockedFlavorRequirement:
    definition = FlavorDefinition(
        coordinate=FlavorCoordinate("samples", "packaging-ovpackage"),
        version="1.0.0",
        display_name="ovpackage",
        primary_axis=FlavorAxis.PACKAGING,
        secondary_constraints=(),
        applicable_capabilities=("portable-application",),
        provides=(),
        requires=(requirement,),
        specification_fragments=(
            reference("specification", "flavors/ovpackage/spec.md"),
        ),
        authoring_inputs=(),
        contributions=(),
        conflicts=(),
        co_requisites=(),
        order_before=(),
        order_after=(),
        supported_targets=("ovpackage",),
    )
    return LockedFlavorRequirement(
        FlavorRevision(definition, identity("ovpackage-source"), ()), requirement
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


def component_lock_chain(size: int = 12) -> ComponentLock:
    authorings = tuple(
        component_authoring(
            f"chain-{index}",
            interface=(
                None if index == 0 else (f"chain-{index}", f"chain-{index}-interface")
            ),
            requirements=(
                ()
                if index == size - 1
                else (
                    CapabilityRequirement(
                        f"dependency-{index}",
                        f"chain-{index + 1}",
                        ">=1,<2",
                        DependencyKind.GENERATION,
                    ),
                )
            ),
        )
        for index in range(size)
    )
    nodes = [
        node(
            locked_revision(
                authoring.coordinate.name,
                selected_flavor="python",
                interface=(
                    None
                    if index == 0
                    else (
                        f"chain-{index}",
                        f"chain-{index}-interface",
                    )
                ),
                authoring=authoring,
            ),
            selected_flavor="python",
            capability=None if index == 0 else f"chain-{index}",
        )
        for index, authoring in enumerate(authorings)
    ]
    edges: list[ExecutableComponentEdge] = []
    for index, consumer in enumerate(nodes[:-1]):
        provider = nodes[index + 1]
        requirement = authorings[index].requires[0]
        edges.append(
            ExecutableComponentEdge(
                consumer_revision=consumer.revision.identity,
                provider_revision=provider.revision.identity,
                requirement_id=requirement.requirement_id,
                capability=requirement.capability,
                kind=requirement.dependency_kind,
                public_interface_identity=(
                    provider.interface_bindings[0].interface_identity
                ),
            )
        )
        nodes[index] = replace(
            consumer,
            requirement_constraint_satisfactions=(
                RequirementConstraintSatisfaction(
                    consumer_revision=consumer.revision.identity,
                    provider_revision=provider.revision.identity,
                    requirement_id=requirement.requirement_id,
                    constraints=requirement.constraints,
                    target_name=consumer.target_flavor_selection.target_name,
                    target_profile_identity=(
                        consumer.target_flavor_selection.target_profile_identity
                    ),
                    selection_policy_identity=(
                        consumer.target_flavor_selection.selection_policy_identity
                    ),
                    target_flavor_selection_identity=(
                        consumer.target_flavor_selection.identity
                    ),
                    satisfaction_evidence_identity=identity(
                        f"chain-{index}-constraint-satisfaction"
                    ),
                ),
            ),
        )
    return ComponentLock(
        target_name="host",
        target_profile_identity=identity("invoice-cli-target"),
        selection_policy_identity=identity("selection-policy"),
        resolver_identity=identity("component-lock-resolver"),
        root_revision=nodes[0].revision.identity,
        nodes=tuple(sorted(nodes, key=lambda item: item.revision.identity.uri)),
        edges=tuple(
            sorted(
                edges,
                key=lambda item: (
                    item.consumer_revision.uri,
                    item.provider_revision.uri,
                    item.requirement_id,
                    item.kind.value,
                ),
            )
        ),
        _authorings=tuple(sorted(authorings, key=lambda item: item.identity.uri)),
    )


class ComponentLockContractTests(unittest.TestCase):
    def test_locked_flavor_requirement_is_exact_canonical_and_selected(self) -> None:
        requirement = CapabilityRequirement(
            "publish-package",
            "sample.publisher",
            ">=1,<2",
            DependencyKind.PACKAGING,
        )
        binding = locked_flavor_requirement(requirement)
        revision = locked_revision("package", selected_flavor="python")
        revision = replace(
            revision,
            selected_flavor_revisions=(binding.flavor_revision.identity,),
            flavor_requirements=(binding,),
        )

        self.assertEqual(LockedFlavorRequirement.from_dict(binding.to_dict()), binding)
        self.assertEqual(
            LockedComponentRevision.from_dict(
                revision.to_dict(), definitions=(revision.definition,)
            ),
            revision,
        )
        schema_catalog().validate(binding.SCHEMA, binding.to_dict())
        schema_catalog().validate(revision.SCHEMA, revision.to_dict())

        forged_requirement = replace(requirement, capability="sample.forged")
        with self.assertRaisesRegex(
            ContractValidationError, "declared by the exact Flavor"
        ):
            replace(binding, requirement=forged_requirement)
        with self.assertRaisesRegex(
            ContractValidationError, "exact selected Flavor revision"
        ):
            replace(
                revision,
                selected_flavor_revisions=(identity("different-flavor"),),
            )
        with self.assertRaisesRegex(ContractValidationError, "must be unique"):
            replace(revision, flavor_requirements=(binding, binding))

    def test_lock_family_round_trips_and_validates_against_schema(self) -> None:
        lock = component_lock()
        wire_value = lock.to_dict()
        self.assertNotIn("authorings", wire_value)
        self.assertNotIn("_authorings", wire_value)
        self.assertNotIn("definition", wire_value["nodes"][0]["revision"])
        self.assertEqual(replace(lock).authorings, lock.authorings)
        restored = ComponentLock.from_dict(wire_value, authorings=lock.authorings)
        self.assertEqual(restored, lock)
        self.assertEqual(
            tuple(node.revision.definition for node in restored.nodes),
            tuple(
                next(
                    authoring
                    for authoring in lock.authorings
                    if authoring.identity == node.revision.authoring_identity
                )
                for node in restored.nodes
            ),
        )
        catalog = schema_catalog()
        catalog.validate(
            LockedComponentRevision.SCHEMA, lock.nodes[0].revision.to_dict()
        )
        catalog.validate(ComponentLockNode.SCHEMA, lock.nodes[0].to_dict())
        catalog.validate(ComponentLock.SCHEMA, lock.to_dict())

    def test_locked_revision_requires_exact_external_definition(self) -> None:
        revision = locked_revision("definition", selected_flavor="python")
        wire = revision.to_dict()

        with self.assertRaises(TypeError):
            LockedComponentRevision.from_dict(wire)  # type: ignore[call-arg]
        with self.assertRaisesRegex(
            ContractValidationError, "exactly one external ComponentAuthoring"
        ):
            LockedComponentRevision.from_dict(wire, definitions=())

        substituted = component_authoring("substituted")
        with self.assertRaisesRegex(
            ContractValidationError, "exactly one external ComponentAuthoring"
        ):
            LockedComponentRevision.from_dict(wire, definitions=(substituted,))
        with self.assertRaisesRegex(
            ContractValidationError, "exact authoring identity"
        ):
            replace(revision, definition=substituted)

    def test_twelve_node_lock_round_trip_retains_typed_definitions(self) -> None:
        lock = component_lock_chain()
        restored = ComponentLock.from_dict(
            lock.to_dict(),
            authorings=lock.authorings,
        )

        self.assertEqual(len(restored.nodes), 12)
        self.assertEqual(len(restored.authorings), 12)
        self.assertTrue(
            all(
                node.revision.definition.identity == node.revision.authoring_identity
                for node in restored.nodes
            )
        )
        self.assertEqual(root_entrypoint_kind(restored), "portable-application")
        self.assertEqual(root_component_name(restored), "chain-0")

    def test_locked_revision_definition_preserves_selector_validation(self) -> None:
        lock = component_lock()
        root = next(
            item for item in lock.nodes if item.revision.identity == lock.root_revision
        )
        forged_reference = replace(
            root.revision.authoring_inputs[0],
            uri="skills/substituted.md",
        )
        forged_revision = replace(
            root.revision,
            authoring_inputs=tuple(
                sorted(
                    (forged_reference, *root.revision.authoring_inputs[1:]),
                    key=lambda item: (item.kind, item.uri),
                )
            ),
        )
        forged_selection = replace(
            root.target_flavor_selection,
            component_revision=forged_revision.identity,
        )
        forged_node = replace(
            root,
            revision=forged_revision,
            target_flavor_selection=forged_selection,
            requirement_constraint_satisfactions=tuple(
                replace(
                    item,
                    consumer_revision=forged_revision.identity,
                    target_flavor_selection_identity=forged_selection.identity,
                )
                for item in root.requirement_constraint_satisfactions
            ),
        )
        nodes = tuple(
            sorted(
                (forged_node, *(item for item in lock.nodes if item is not root)),
                key=lambda item: item.revision.identity.uri,
            )
        )
        edges = tuple(
            replace(edge, consumer_revision=forged_revision.identity)
            if edge.consumer_revision == root.revision.identity
            else edge
            for edge in lock.edges
        )
        with self.assertRaisesRegex(ContractValidationError, "authored selector"):
            replace(
                lock,
                root_revision=forged_revision.identity,
                nodes=nodes,
                edges=edges,
            )

    def test_rejected_catalog_change_changes_audit_only(self) -> None:
        lock = component_lock()
        root = lock.root_revision
        selected = NodeFlavorCandidateAudit(
            root,
            "language",
            identity("flavor-rust"),
            CandidateStatus.SELECTED,
        )
        rejected = NodeFlavorCandidateAudit(
            root,
            "language",
            identity("flavor-zig"),
            CandidateStatus.REJECTED,
            ("target constraint mismatch",),
        )
        candidates = tuple(
            sorted(
                (selected, rejected),
                key=lambda item: (
                    item.component_revision.uri,
                    item.slot_id,
                    item.flavor_revision.uri,
                ),
            )
        )
        before = ComponentResolutionAudit(
            lock.identity,
            identity("catalog-before"),
            lock.resolver_identity,
            candidates,
        )
        replacement = replace(
            rejected,
            flavor_revision=identity("flavor-new-unrelated"),
            reasons=("not selected by the target",),
        )
        after = replace(
            before,
            catalog_identity=identity("catalog-after"),
            candidates=tuple(
                sorted(
                    (selected, replacement),
                    key=lambda item: (
                        item.component_revision.uri,
                        item.slot_id,
                        item.flavor_revision.uri,
                    ),
                )
            ),
        )

        self.assertNotEqual(before.identity, after.identity)
        self.assertEqual(before.component_lock_identity, after.component_lock_identity)
        self.assertEqual(lock.identity, lock.selected_derivation_identity)
        self.assertNotIn("candidates", lock.to_dict())
        self.assertEqual(ComponentResolutionAudit.from_dict(before.to_dict()), before)
        schema_catalog().validate(ComponentResolutionAudit.SCHEMA, before.to_dict())

    def test_selected_flavor_changes_locked_revision_and_lock(self) -> None:
        before = component_lock()
        root_index = next(
            index
            for index, item in enumerate(before.nodes)
            if item.revision.identity == before.root_revision
        )
        root = before.nodes[root_index]
        changed_slot = replace(
            root.target_flavor_selection.slots[0],
            selected=(SelectedNodeFlavor("cpp", identity("flavor-cpp")),),
        )
        changed_revision = replace(
            root.revision,
            selected_flavor_revisions=(identity("flavor-cpp"),),
        )
        changed_selection = replace(
            root.target_flavor_selection,
            component_revision=changed_revision.identity,
            slots=(changed_slot,),
        )
        changed_node = ComponentLockNode(
            changed_revision,
            changed_selection,
            (),
            tuple(
                replace(
                    item,
                    consumer_revision=changed_revision.identity,
                    target_flavor_selection_identity=changed_selection.identity,
                )
                for item in root.requirement_constraint_satisfactions
            ),
        )
        nodes = [*before.nodes]
        nodes[root_index] = changed_node
        nodes = sorted(nodes, key=lambda item: item.revision.identity.uri)
        changed_edges = tuple(
            sorted(
                (
                    replace(
                        edge,
                        consumer_revision=changed_revision.identity,
                    )
                    if edge.consumer_revision == root.revision.identity
                    else edge
                    for edge in before.edges
                ),
                key=lambda item: (
                    item.consumer_revision.uri,
                    item.provider_revision.uri,
                    item.requirement_id,
                    item.kind.value,
                ),
            )
        )
        after = replace(
            before,
            root_revision=changed_revision.identity,
            nodes=tuple(nodes),
            edges=changed_edges,
        )

        self.assertNotEqual(root.revision.identity, changed_node.revision.identity)
        self.assertNotEqual(before.identity, after.identity)
        self.assertNotEqual(
            root.target_flavor_selection.identity,
            changed_node.target_flavor_selection.identity,
        )

    def test_lock_rejects_noncanonical_missing_cyclic_and_forged_graphs(self) -> None:
        lock = component_lock()
        with self.assertRaisesRegex(ContractValidationError, "canonical locked"):
            replace(lock, nodes=tuple(reversed(lock.nodes)))
        with self.assertRaisesRegex(ContractValidationError, "must identify a node"):
            replace(lock, root_revision=identity("missing-root"))

        outside = ExecutableComponentEdge(
            lock.root_revision,
            identity("outside"),
            "outside-runtime",
            "outside-api",
            DependencyKind.RUNTIME,
            None,
        )
        with self.assertRaisesRegex(ContractValidationError, "outside the graph"):
            replace(lock, edges=(outside,))

        provider = next(
            item for item in lock.nodes if item.revision.identity != lock.root_revision
        )
        reverse = ExecutableComponentEdge(
            provider.revision.identity,
            lock.root_revision,
            "invoice-runtime",
            "invoice-runtime",
            DependencyKind.RUNTIME,
            None,
        )
        with self.assertRaisesRegex(ContractValidationError, "acyclic"):
            _require_closed_acyclic_graph(
                tuple(item.revision.identity.uri for item in lock.nodes),
                (*lock.edges, reverse),
                root_revision=lock.root_revision.uri,
            )

        forged = replace(
            lock.edges[0],
            public_interface_identity=identity("forged-interface"),
        )
        with self.assertRaisesRegex(ContractValidationError, "exact capability"):
            replace(lock, edges=(forged,))

        original_mixed = lock.nodes[0]
        mixed_selection = replace(
            original_mixed.target_flavor_selection,
            target_profile_identity=identity("another-target-profile"),
        )
        mixed_target = replace(
            original_mixed,
            target_flavor_selection=mixed_selection,
            requirement_constraint_satisfactions=tuple(
                replace(
                    item,
                    target_profile_identity=mixed_selection.target_profile_identity,
                    target_flavor_selection_identity=mixed_selection.identity,
                )
                for item in original_mixed.requirement_constraint_satisfactions
            ),
        )
        mixed_nodes = tuple(
            sorted(
                (mixed_target, *lock.nodes[1:]),
                key=lambda item: item.revision.identity.uri,
            )
        )
        with self.assertRaisesRegex(ContractValidationError, "exact target and policy"):
            replace(lock, nodes=mixed_nodes)

    def test_interface_bindings_are_complete_and_cannot_create_identity_cycle(
        self,
    ) -> None:
        lock = component_lock()
        provider = next(item for item in lock.nodes if item.interface_bindings)
        self.assertNotIn("interface_bindings", provider.revision.to_dict())
        with self.assertRaisesRegex(ContractValidationError, "every and only locked"):
            replace(provider, interface_bindings=())
        with self.assertRaisesRegex(ContractValidationError, "enclosing locked"):
            replace(
                provider,
                interface_bindings=(
                    replace(
                        provider.interface_bindings[0],
                        component_revision=identity("another-revision"),
                    ),
                ),
            )

    def test_runtime_and_provenance_evidence_are_not_lock_fields(self) -> None:
        forbidden = (
            "source_snapshot",
            "parent_revisions",
            "parent_runs",
            "generation_prompt",
            "test_receipt",
            "promotion_provenance",
            "catalog_candidates",
        )
        revision = component_lock().nodes[0].revision
        for field in forbidden:
            with self.subTest(field=field):
                value = revision.to_dict()
                value[field] = identity(field).to_dict()
                with self.assertRaisesRegex(ContractValidationError, "unknown fields"):
                    LockedComponentRevision.from_dict(
                        value, definitions=(revision.definition,)
                    )

    def test_lock_requires_exact_external_authoring_and_specification_binding(
        self,
    ) -> None:
        lock = component_lock()
        with self.assertRaises(TypeError):
            ComponentLock.from_dict(lock.to_dict())  # type: ignore[call-arg]

        root = next(
            item for item in lock.nodes if item.revision.identity == lock.root_revision
        )
        forged_revision = replace(
            root.revision,
            specification_set_identity=identity("unrelated-specification-set"),
        )
        forged_selection = replace(
            root.target_flavor_selection,
            component_revision=forged_revision.identity,
        )
        forged_node = replace(
            root,
            revision=forged_revision,
            target_flavor_selection=forged_selection,
            requirement_constraint_satisfactions=tuple(
                replace(
                    item,
                    consumer_revision=forged_revision.identity,
                    target_flavor_selection_identity=forged_selection.identity,
                )
                for item in root.requirement_constraint_satisfactions
            ),
        )
        forged_nodes = tuple(
            sorted(
                (forged_node, *(item for item in lock.nodes if item is not root)),
                key=lambda item: item.revision.identity.uri,
            )
        )
        forged_edges = tuple(
            replace(edge, consumer_revision=forged_revision.identity)
            if edge.consumer_revision == root.revision.identity
            else edge
            for edge in lock.edges
        )
        with self.assertRaisesRegex(
            ContractValidationError, "exact provider and ordered specification"
        ):
            replace(
                lock,
                root_revision=forged_revision.identity,
                nodes=forged_nodes,
                edges=forged_edges,
            )

        first = reference("specification", "specs/first.md")
        second = reference("specification", "specs/second.md")
        self.assertNotEqual(
            ordered_specification_set_identity("literate-markdown", (first, second)),
            ordered_specification_set_identity("literate-markdown", (second, first)),
        )

        too_many = tuple(
            sorted(
                (identity(f"flavor-{index}") for index in range(257)),
                key=lambda item: item.uri,
            )
        )
        with self.assertRaisesRegex(ContractValidationError, "at most 256"):
            replace(root.revision, selected_flavor_revisions=too_many)

    def test_requirement_constraint_satisfaction_is_mandatory_and_identity_bound(
        self,
    ) -> None:
        lock = component_lock()
        root = next(
            item for item in lock.nodes if item.revision.identity == lock.root_revision
        )
        satisfaction = root.requirement_constraint_satisfactions[0]
        self.assertEqual(satisfaction.constraints, ())

        def with_root(replacement: ComponentLockNode) -> tuple[ComponentLockNode, ...]:
            return tuple(
                sorted(
                    (
                        replacement,
                        *(item for item in lock.nodes if item is not root),
                    ),
                    key=lambda item: item.revision.identity.uri,
                )
            )

        without_evidence = replace(root, requirement_constraint_satisfactions=())
        with self.assertRaisesRegex(
            ContractValidationError, "constraint satisfaction evidence"
        ):
            replace(lock, nodes=with_root(without_evidence))

        wrong_provider = replace(
            root,
            requirement_constraint_satisfactions=(
                replace(satisfaction, provider_revision=identity("wrong-provider")),
            ),
        )
        with self.assertRaisesRegex(ContractValidationError, "selected provider"):
            replace(lock, nodes=with_root(wrong_provider))

        with self.assertRaisesRegex(ContractValidationError, "exact target/Flavor"):
            replace(
                root,
                requirement_constraint_satisfactions=(
                    replace(
                        satisfaction,
                        target_profile_identity=identity("wrong-profile"),
                    ),
                ),
            )

        changed_evidence = replace(
            root,
            requirement_constraint_satisfactions=(
                replace(
                    satisfaction,
                    satisfaction_evidence_identity=identity("new-evidence"),
                ),
            ),
        )
        changed_lock = replace(lock, nodes=with_root(changed_evidence))
        self.assertNotEqual(changed_lock.identity, lock.identity)

    def test_requirement_constraints_are_covered_exactly(self) -> None:
        lock = component_lock()
        root = next(
            item for item in lock.nodes if item.revision.identity == lock.root_revision
        )
        root_authoring = next(
            item
            for item in lock.authorings
            if item.identity == root.revision.authoring_identity
        )
        constraint = CapabilityConstraint("platform", "equals", ("host",))
        constrained_requirement = replace(
            root_authoring.requires[0], constraints=(constraint,)
        )
        constrained_authoring = replace(
            root_authoring, requires=(constrained_requirement,)
        )
        constrained_revision = replace(
            root.revision,
            authoring_identity=constrained_authoring.identity,
            definition=constrained_authoring,
        )
        constrained_selection = replace(
            root.target_flavor_selection,
            component_revision=constrained_revision.identity,
        )
        provider_revision = lock.edges[0].provider_revision
        constrained_satisfaction = RequirementConstraintSatisfaction(
            consumer_revision=constrained_revision.identity,
            provider_revision=provider_revision,
            requirement_id=constrained_requirement.requirement_id,
            constraints=(constraint,),
            target_name=constrained_selection.target_name,
            target_profile_identity=constrained_selection.target_profile_identity,
            selection_policy_identity=constrained_selection.selection_policy_identity,
            target_flavor_selection_identity=constrained_selection.identity,
            satisfaction_evidence_identity=identity("platform-satisfied"),
        )
        constrained_root = ComponentLockNode(
            constrained_revision,
            constrained_selection,
            root.interface_bindings,
            (constrained_satisfaction,),
        )
        nodes = tuple(
            sorted(
                (
                    constrained_root,
                    *(item for item in lock.nodes if item is not root),
                ),
                key=lambda item: item.revision.identity.uri,
            )
        )
        edges = tuple(
            replace(edge, consumer_revision=constrained_revision.identity)
            if edge.consumer_revision == root.revision.identity
            else edge
            for edge in lock.edges
        )
        authorings = tuple(
            sorted(
                (
                    constrained_authoring,
                    *(item for item in lock.authorings if item is not root_authoring),
                ),
                key=lambda item: item.identity.uri,
            )
        )
        constrained_lock = replace(
            lock,
            root_revision=constrained_revision.identity,
            nodes=nodes,
            edges=edges,
            _authorings=authorings,
        )
        schema_catalog().validate(ComponentLock.SCHEMA, constrained_lock.to_dict())
        self.assertEqual(
            ComponentLock.from_dict(
                constrained_lock.to_dict(), authorings=constrained_lock.authorings
            ),
            constrained_lock,
        )

        uncovered = replace(
            constrained_root,
            requirement_constraint_satisfactions=(
                replace(constrained_satisfaction, constraints=()),
            ),
        )
        uncovered_nodes = tuple(
            sorted(
                (uncovered, *(item for item in nodes if item is not constrained_root)),
                key=lambda item: item.revision.identity.uri,
            )
        )
        with self.assertRaisesRegex(ContractValidationError, "every and only authored"):
            replace(constrained_lock, nodes=uncovered_nodes)

    def test_lock_rejects_missing_slots_disconnected_nodes_and_invented_edges(
        self,
    ) -> None:
        lock = component_lock()
        root = next(
            item for item in lock.nodes if item.revision.identity == lock.root_revision
        )
        slotless_revision = replace(root.revision, selected_flavor_revisions=())
        slotless_selection = replace(
            root.target_flavor_selection,
            component_revision=slotless_revision.identity,
            slots=(),
        )
        slotless = ComponentLockNode(
            slotless_revision,
            slotless_selection,
            (),
            tuple(
                replace(
                    item,
                    consumer_revision=slotless_revision.identity,
                    target_flavor_selection_identity=slotless_selection.identity,
                )
                for item in root.requirement_constraint_satisfactions
            ),
        )
        slotless_nodes = tuple(
            sorted(
                (slotless, *(item for item in lock.nodes if item is not root)),
                key=lambda item: item.revision.identity.uri,
            )
        )
        slotless_edges = tuple(
            replace(edge, consumer_revision=slotless_revision.identity)
            if edge.consumer_revision == root.revision.identity
            else edge
            for edge in lock.edges
        )
        with self.assertRaisesRegex(ContractValidationError, "every and only declared"):
            replace(
                lock,
                root_revision=slotless_revision.identity,
                nodes=slotless_nodes,
                edges=slotless_edges,
            )

        orphan_authoring = component_authoring("orphan")
        orphan = node(
            locked_revision(
                "orphan", selected_flavor="rust", authoring=orphan_authoring
            ),
            selected_flavor="rust",
            target_profile_identity=lock.target_profile_identity,
        )
        with self.assertRaisesRegex(ContractValidationError, "reachable"):
            replace(
                lock,
                nodes=tuple(
                    sorted(
                        (*lock.nodes, orphan),
                        key=lambda item: item.revision.identity.uri,
                    )
                ),
                _authorings=tuple(
                    sorted(
                        (*lock.authorings, orphan_authoring),
                        key=lambda item: item.identity.uri,
                    )
                ),
            )

        arbitrary = replace(
            lock.edges[0],
            requirement_id="invented",
            capability="invented-api",
        )
        with self.assertRaisesRegex(ContractValidationError, "cannot invent"):
            replace(lock, edges=(arbitrary,))

    def test_one_requirement_cannot_select_two_providers(self) -> None:
        lock = component_lock()
        edge = lock.edges[0]
        third_authoring = component_authoring(
            "pricing-two", interface=("pricing-api", "pricing-two-interface")
        )
        third = node(
            locked_revision(
                "pricing-two",
                selected_flavor="python",
                interface=("pricing-api", "pricing-two-interface"),
                authoring=third_authoring,
            ),
            selected_flavor="python",
            capability="pricing-api",
            target_profile_identity=lock.target_profile_identity,
        )
        duplicate = replace(
            edge,
            provider_revision=third.revision.identity,
            public_interface_identity=third.interface_bindings[0].interface_identity,
        )
        edges = tuple(
            sorted(
                (*lock.edges, duplicate),
                key=lambda item: (
                    item.consumer_revision.uri,
                    item.provider_revision.uri,
                    item.requirement_id,
                    item.kind.value,
                ),
            )
        )
        with self.assertRaisesRegex(ContractValidationError, "multiple providers"):
            replace(
                lock,
                nodes=tuple(
                    sorted(
                        (*lock.nodes, third),
                        key=lambda item: item.revision.identity.uri,
                    )
                ),
                edges=edges,
                _authorings=tuple(
                    sorted(
                        (*lock.authorings, third_authoring),
                        key=lambda item: item.identity.uri,
                    )
                ),
            )

    def test_repository_lock_and_selector_pin_must_match_authoring(self) -> None:
        expected_integration = identity("expected-integration")
        authored_dependency = AuthoredRepositorySourceDependency(
            "sqlite",
            "https://github.com/sqlite/sqlite.git",
            RepositoryRevisionSelector(RepositoryRevisionKind.BRANCH, "trunk"),
            DependencyKind.BUILD,
            integration_contract=selector(
                "integration-contract",
                "dependencies/sqlite.md",
                pin=expected_integration,
            ),
        )
        authoring = replace(
            component_authoring("repository-user"),
            source_dependencies=(authored_dependency,),
        )

        def locked_source(
            integration_identity: ContentIdentity,
            *,
            repository_url: str = "https://github.com/sqlite/sqlite.git",
        ) -> RepositorySourceLock:
            return RepositorySourceLock(
                RepositorySourceDependency(
                    "sqlite",
                    repository_url,
                    authored_dependency.revision_selector,
                    DependencyKind.BUILD,
                    False,
                    ContentReference(
                        "integration-contract",
                        "dependencies/sqlite.md",
                        integration_identity,
                    ),
                ),
                "a" * 40,
                identity("repository-snapshot"),
                identity("repository-tree"),
                identity("repository-resolver"),
            )

        base_revision = locked_revision(
            "repository-user", selected_flavor="rust", authoring=authoring
        )
        forged_revision = replace(
            base_revision,
            repository_sources=(locked_source(identity("wrong-integration")),),
        )
        forged_node = node(forged_revision, selected_flavor="rust")
        with self.assertRaisesRegex(ContractValidationError, "selector pin"):
            _require_node_authoring(forged_node, authoring)

        valid_revision = replace(
            base_revision,
            repository_sources=(locked_source(expected_integration),),
        )
        _require_node_authoring(node(valid_revision, selected_flavor="rust"), authoring)

        wrong_repository = replace(
            base_revision,
            repository_sources=(
                locked_source(
                    expected_integration,
                    repository_url="https://example.com/not-sqlite.git",
                ),
            ),
        )
        with self.assertRaisesRegex(ContractValidationError, "repository selector"):
            _require_node_authoring(
                node(wrong_repository, selected_flavor="rust"), authoring
            )

    def test_iterative_dag_validation_handles_the_contract_bound(self) -> None:
        revisions = tuple(identity(f"node-{index}") for index in range(4096))
        edges = tuple(
            ExecutableComponentEdge(
                revisions[index],
                revisions[index + 1],
                f"dependency-{index}",
                "chain-api",
                DependencyKind.RUNTIME,
                None,
            )
            for index in range(len(revisions) - 1)
        )
        _require_closed_acyclic_graph(
            tuple(item.uri for item in revisions),
            edges,
            root_revision=revisions[0].uri,
        )


if __name__ == "__main__":
    unittest.main()
