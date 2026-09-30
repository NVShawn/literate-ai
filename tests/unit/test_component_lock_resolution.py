"""Pure selected Component-lock resolver application-service tests."""

from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from literate_ai.application.component_lock_resolution import (
    CatalogAttribute,
    ComponentLockResolutionError,
    ComponentLockResolutionPlan,
    ComponentLockResolver,
    FlavorCandidateInput,
    RequirementProviderInput,
    ResolvedComponentNodeInput,
)
from literate_ai.contracts.capabilities import (
    CapabilityConstraint,
    CapabilityRequirement,
    DependencyKind,
)
from literate_ai.contracts.flavors import CandidateStatus
from literate_ai.contracts.repositories import RepositorySourceLock
from tests.unit.test_component_lock_contracts import (
    component_authoring,
    identity,
    reference,
)


def resolved_node(
    authoring,
    *,
    selected: str,
    rejected: str,
    attributes: tuple[CatalogAttribute, ...] = (),
    repository_sources: tuple[RepositorySourceLock, ...] = (),
) -> ResolvedComponentNodeInput:
    interfaces = tuple(
        reference(
            "public-interface-contract",
            provided.interface.uri,
            f"{provided.name}-interface",
        )
        for provided in authoring.provides
        if provided.interface is not None
    )
    return ResolvedComponentNodeInput(
        authoring=authoring,
        specifications=tuple(
            reference("specification", uri, f"specification-{uri}")
            for uri in authoring.specification_roots
        ),
        authoring_inputs=tuple(
            reference(selector.kind, selector.uri, f"authoring-input-{index}")
            for index, selector in enumerate(authoring.authoring_inputs)
        ),
        workflow_definition=reference("workflow", "workflows/host.json", "workflow"),
        routing_policy=reference("routing-policy", "routing/default.json", "routing"),
        acceptance_contracts=(
            reference("acceptance-contract", "acceptance/execution.json", "acceptance"),
        ),
        repository_sources=repository_sources,
        public_interfaces=interfaces,
        flavor_candidates=(
            FlavorCandidateInput(
                "language",
                rejected,
                identity(f"flavor-{rejected}"),
                CandidateStatus.REJECTED,
                ("not selected by target",),
            ),
            FlavorCandidateInput(
                "language",
                selected,
                identity(f"flavor-{selected}"),
                CandidateStatus.SELECTED,
            ),
        ),
        catalog_attributes=attributes,
    )


def resolution_plan() -> ComponentLockResolutionPlan:
    requirement = CapabilityRequirement(
        "pricing",
        "pricing-api",
        ">=1,<2",
        DependencyKind.GENERATION,
        constraints=(CapabilityConstraint("regions", "contains-all", ("eu", "us")),),
    )
    consumer = component_authoring("invoice-cli", requirements=(requirement,))
    provider = component_authoring(
        "pricing", interface=("pricing-api", "pricing-interface")
    )
    return ComponentLockResolutionPlan(
        target_name="host",
        target_profile_identity=identity("target-profile"),
        selection_policy_identity=identity("selection-policy"),
        resolver_identity=identity("component-lock-resolver"),
        catalog_identity=identity("catalog"),
        root_authoring_identity=consumer.identity,
        # Deliberately reverse discovery order; resolution must canonicalize it.
        nodes=(
            resolved_node(
                provider,
                selected="python",
                rejected="rust",
                attributes=(CatalogAttribute("regions", ("us", "eu")),),
            ),
            resolved_node(consumer, selected="rust", rejected="python"),
        ),
        requirement_providers=(
            RequirementProviderInput(
                consumer.identity,
                "pricing",
                provider.identity,
            ),
        ),
    )


class ComponentLockResolutionTests(unittest.TestCase):
    def test_resolution_plan_wire_round_trip_is_canonical(self) -> None:
        plan = resolution_plan()
        canonical = plan.to_dict()
        reordered = copy.deepcopy(canonical)
        reordered["nodes"].reverse()
        for node in reordered["nodes"]:
            node["flavor_candidates"].reverse()
            node["catalog_attributes"].reverse()
            for attribute in node["catalog_attributes"]:
                attribute["values"].reverse()
        reordered["requirement_providers"].reverse()

        decoded = ComponentLockResolutionPlan.from_dict(reordered)

        self.assertEqual(decoded.to_dict(), canonical)
        self.assertEqual(decoded.identity, plan.identity)
        result = ComponentLockResolver().resolve(
            decoded, expected_input_evidence_identity=decoded.identity
        )
        self.assertEqual(result.input_evidence_identity, plan.identity)

    def test_resolution_plan_wire_rejects_unknown_fields_at_every_input_layer(
        self,
    ) -> None:
        canonical = resolution_plan().to_dict()
        mutations = []

        top = copy.deepcopy(canonical)
        top["future"] = True
        mutations.append(top)

        node = copy.deepcopy(canonical)
        node["nodes"][0]["future"] = True
        mutations.append(node)

        candidate = copy.deepcopy(canonical)
        candidate["nodes"][0]["flavor_candidates"][0]["future"] = True
        mutations.append(candidate)

        attribute = copy.deepcopy(canonical)
        attributed_node = next(
            item for item in attribute["nodes"] if item["catalog_attributes"]
        )
        attributed_node["catalog_attributes"][0]["future"] = True
        mutations.append(attribute)

        provider = copy.deepcopy(canonical)
        provider["requirement_providers"][0]["future"] = True
        mutations.append(provider)

        authored = copy.deepcopy(canonical)
        authored["nodes"][0]["authoring"]["future"] = True
        mutations.append(authored)

        for value in mutations:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "unknown fields|unknown field"):
                    ComponentLockResolutionPlan.from_dict(value)

    def test_resolution_plan_wire_rejects_future_schema_and_malformed_values(
        self,
    ) -> None:
        canonical = resolution_plan().to_dict()
        future = copy.deepcopy(canonical)
        future["schema"] = "literate-ai/component-lock-resolution-plan@2"
        with self.assertRaisesRegex(
            ComponentLockResolutionError, "schema must be"
        ) as raised:
            ComponentLockResolutionPlan.from_dict(future)
        self.assertEqual(raised.exception.code, "unsupported-resolution-plan-schema")

        mutations = []
        missing = copy.deepcopy(canonical)
        del missing["catalog_identity"]
        mutations.append(missing)

        nodes_not_array = copy.deepcopy(canonical)
        nodes_not_array["nodes"] = {}
        mutations.append(nodes_not_array)

        bad_identity = copy.deepcopy(canonical)
        bad_identity["resolver_identity"]["digest"] = "not-a-digest"
        mutations.append(bad_identity)

        bad_status = copy.deepcopy(canonical)
        bad_status["nodes"][0]["flavor_candidates"][0]["status"] = "future"
        mutations.append(bad_status)

        duplicate_reasons = copy.deepcopy(canonical)
        rejected = next(
            item
            for item in duplicate_reasons["nodes"][0]["flavor_candidates"]
            if item["status"] != "selected"
        )
        rejected["reasons"] = ["same", "same"]
        mutations.append(duplicate_reasons)

        future_authoring = copy.deepcopy(canonical)
        future_authoring["nodes"][0]["authoring"]["schema"] = (
            "urn:literate-ai:schema:v3:component-authoring"
        )
        mutations.append(future_authoring)

        for value in mutations:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ComponentLockResolutionPlan.from_dict(value)

    def test_resolves_exact_target_flavors_capability_and_constraints(self) -> None:
        plan = resolution_plan()
        result = ComponentLockResolver().resolve(
            plan, expected_input_evidence_identity=plan.identity
        )

        self.assertEqual(result.input_evidence_identity, plan.identity)
        self.assertEqual(
            result.catalog_audit.component_lock_identity, result.lock.identity
        )
        self.assertEqual(
            tuple(item.revision.identity.uri for item in result.lock.nodes),
            tuple(sorted(item.revision.identity.uri for item in result.lock.nodes)),
        )
        self.assertEqual(
            {item.target_flavor_selection.target_name for item in result.lock.nodes},
            {"host"},
        )
        selected = {
            item.revision.coordinate.name: tuple(
                flavor.value
                for slot in item.target_flavor_selection.slots
                for flavor in slot.selected
            )
            for item in result.lock.nodes
        }
        self.assertEqual(selected, {"invoice-cli": ("rust",), "pricing": ("python",)})
        self.assertEqual(result.lock.edges[0].capability, "pricing-api")
        satisfaction = next(
            item.requirement_constraint_satisfactions[0]
            for item in result.lock.nodes
            if item.revision.coordinate.name == "invoice-cli"
        )
        self.assertEqual(satisfaction.constraints[0].key, "regions")

    def test_unselected_catalog_change_changes_audit_not_selected_lock(self) -> None:
        before_plan = resolution_plan()
        before = ComponentLockResolver().resolve(
            before_plan, expected_input_evidence_identity=before_plan.identity
        )
        provider = before_plan.nodes[0]
        candidates = tuple(
            replace(
                item,
                flavor_revision=identity("flavor-zig"),
                value="zig",
                reasons=("new rejected catalog candidate",),
            )
            if item.status is CandidateStatus.REJECTED
            else item
            for item in provider.flavor_candidates
        )
        after_plan = replace(
            before_plan,
            catalog_identity=identity("catalog-updated"),
            nodes=(
                replace(provider, flavor_candidates=candidates),
                before_plan.nodes[1],
            ),
        )
        after = ComponentLockResolver().resolve(
            after_plan, expected_input_evidence_identity=after_plan.identity
        )

        self.assertEqual(before.lock.identity, after.lock.identity)
        self.assertNotEqual(before.catalog_audit.identity, after.catalog_audit.identity)
        self.assertNotEqual(
            before.input_evidence_identity, after.input_evidence_identity
        )

    def test_input_order_does_not_change_plan_lock_or_audit_identity(self) -> None:
        before_plan = resolution_plan()
        reordered_nodes = tuple(
            replace(node, flavor_candidates=tuple(reversed(node.flavor_candidates)))
            for node in reversed(before_plan.nodes)
        )
        after_plan = replace(before_plan, nodes=reordered_nodes)
        resolver = ComponentLockResolver()
        before = resolver.resolve(
            before_plan, expected_input_evidence_identity=before_plan.identity
        )
        after = resolver.resolve(
            after_plan, expected_input_evidence_identity=after_plan.identity
        )

        self.assertEqual(before_plan.identity, after_plan.identity)
        self.assertEqual(before.lock.identity, after.lock.identity)
        self.assertEqual(before.catalog_audit.identity, after.catalog_audit.identity)

    def test_stale_input_and_unsatisfied_constraint_fail_closed(self) -> None:
        before = resolution_plan()
        changed = replace(before, catalog_identity=identity("new-catalog"))
        resolver = ComponentLockResolver()
        with self.assertRaisesRegex(
            ComponentLockResolutionError, "changed after planning"
        ) as stale:
            resolver.resolve(changed, expected_input_evidence_identity=before.identity)
        self.assertEqual(stale.exception.code, "stale-resolution-input")

        provider = replace(
            before.nodes[0],
            catalog_attributes=(CatalogAttribute("regions", ("us",)),),
        )
        unsatisfied = replace(before, nodes=(provider, before.nodes[1]))
        with self.assertRaisesRegex(ComponentLockResolutionError, "does not satisfy"):
            resolver.resolve(
                unsatisfied,
                expected_input_evidence_identity=unsatisfied.identity,
            )


if __name__ == "__main__":
    unittest.main()
