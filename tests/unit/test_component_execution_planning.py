"""Per-Component generation-key and deterministic action-DAG tests."""

from __future__ import annotations

import unittest
from dataclasses import replace

from literate_ai.application.component_execution_planning import (
    ComponentExecutionPlanningError,
    authored_assets_from_lock,
    plan_component_execution,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.capabilities import CapabilityRequirement, DependencyKind
from literate_ai.contracts.component_locking import (
    ComponentAssetSelector,
    ComponentLock,
    RequirementConstraintSatisfaction,
    ResolvedComponentAsset,
)
from literate_ai.contracts.executable_components import (
    AuthoredBinaryAsset,
    ComponentActionPhase,
    ComponentExecutionPlan,
    ExecutableComponentEdge,
)
from literate_ai.contracts.identity import ContentIdentity
from tests.unit.test_component_lock_contracts import (
    component_authoring,
    identity,
    locked_revision,
    node,
)


def _requirement(
    requirement_id: str,
    capability: str,
    dependency_kind: DependencyKind = DependencyKind.GENERATION,
) -> CapabilityRequirement:
    return CapabilityRequirement(
        requirement_id,
        capability,
        ">=1,<2",
        dependency_kind,
    )


def _diamond_lock(
    *,
    money_spec: str = "money-specification",
    money_interface: str = "money-interface",
    invoice_spec: str = "invoice-specification",
    include_invoice_money_packaging_edge: bool = False,
    dependency_kind: DependencyKind = DependencyKind.GENERATION,
    independent_reporting: bool = False,
) -> ComponentLock:
    money_authoring = component_authoring(
        "money", interface=("money-api", "unused-selector-label")
    )
    pricing_authoring = component_authoring(
        "pricing",
        interface=("pricing-api", "unused-selector-label"),
        requirements=(_requirement("money", "money-api", dependency_kind),),
    )
    reporting_authoring = component_authoring(
        "reporting",
        interface=("reporting-api", "unused-selector-label"),
        requirements=(
            ()
            if independent_reporting
            else (_requirement("money", "money-api", dependency_kind),)
        ),
    )
    invoice_requirements = (
        _requirement("pricing", "pricing-api", dependency_kind),
        _requirement("reporting", "reporting-api", dependency_kind),
    )
    if include_invoice_money_packaging_edge:
        # A root Component with both a generation requirement (through pricing and
        # reporting) and a distinct packaging-only requirement on the same provider
        # (money) -- the exact shape that regressed in issue #110.
        invoice_requirements = tuple(
            sorted(
                (
                    *invoice_requirements,
                    _requirement(
                        "money-package", "money-api", DependencyKind.PACKAGING
                    ),
                ),
                key=lambda item: item.requirement_id,
            )
        )
    invoice_authoring = component_authoring(
        "invoice-cli",
        requirements=invoice_requirements,
    )

    money_revision = locked_revision(
        "money",
        selected_flavor="python",
        interface=("money-api", money_interface),
        authoring=money_authoring,
    )
    money_revision = replace(
        money_revision,
        specifications=(
            replace(money_revision.specifications[0], identity=identity(money_spec)),
        ),
    )
    # The ordered-set identity is intentionally exact and must follow a spec change.
    from literate_ai.contracts.component_locking import (
        ordered_specification_set_identity,
    )

    money_revision = replace(
        money_revision,
        specification_set_identity=ordered_specification_set_identity(
            money_authoring.specification_provider, money_revision.specifications
        ),
    )
    invoice_revision = locked_revision(
        "invoice-cli", selected_flavor="rust", authoring=invoice_authoring
    )
    invoice_revision = replace(
        invoice_revision,
        specifications=(
            replace(
                invoice_revision.specifications[0], identity=identity(invoice_spec)
            ),
        ),
    )
    invoice_revision = replace(
        invoice_revision,
        specification_set_identity=ordered_specification_set_identity(
            invoice_authoring.specification_provider, invoice_revision.specifications
        ),
    )
    pricing_revision = locked_revision(
        "pricing",
        selected_flavor="python",
        interface=("pricing-api", "pricing-interface"),
        authoring=pricing_authoring,
    )
    reporting_revision = locked_revision(
        "reporting",
        selected_flavor="javascript",
        interface=("reporting-api", "reporting-interface"),
        authoring=reporting_authoring,
    )
    nodes = {
        "invoice": node(invoice_revision, selected_flavor="rust"),
        "pricing": node(
            pricing_revision, selected_flavor="python", capability="pricing-api"
        ),
        "reporting": node(
            reporting_revision,
            selected_flavor="javascript",
            capability="reporting-api",
        ),
        "money": node(money_revision, selected_flavor="python", capability="money-api"),
    }
    relationships = (
        ("invoice", "pricing", "pricing", "pricing-api"),
        ("invoice", "reporting", "reporting", "reporting-api"),
        ("pricing", "money", "money", "money-api"),
        ("reporting", "money", "money", "money-api"),
    )
    if independent_reporting:
        relationships = relationships[:-1]
    if include_invoice_money_packaging_edge:
        relationships = (
            *relationships,
            ("invoice", "money", "money-package", "money-api"),
        )
    requirements = {
        "invoice": invoice_authoring.requires,
        "pricing": pricing_authoring.requires,
        "reporting": reporting_authoring.requires,
    }
    edges: list[ExecutableComponentEdge] = []
    satisfactions: dict[str, list[RequirementConstraintSatisfaction]] = {
        name: [] for name in nodes
    }
    for consumer_name, provider_name, requirement_id, capability in relationships:
        consumer = nodes[consumer_name]
        provider = nodes[provider_name]
        requirement = next(
            item
            for item in requirements[consumer_name]
            if item.requirement_id == requirement_id
        )
        edges.append(
            ExecutableComponentEdge(
                consumer.revision.identity,
                provider.revision.identity,
                requirement_id,
                capability,
                requirement.dependency_kind,
                (
                    provider.interface_bindings[0].interface_identity
                    if requirement.dependency_kind is DependencyKind.GENERATION
                    else None
                ),
            )
        )
        selection = consumer.target_flavor_selection
        satisfactions[consumer_name].append(
            RequirementConstraintSatisfaction(
                consumer.revision.identity,
                provider.revision.identity,
                requirement_id,
                requirement.constraints,
                selection.target_name,
                selection.target_profile_identity,
                selection.selection_policy_identity,
                selection.identity,
                identity(f"{consumer_name}-{requirement_id}-satisfaction"),
            )
        )
    for name, values in satisfactions.items():
        nodes[name] = replace(
            nodes[name],
            requirement_constraint_satisfactions=tuple(
                sorted(values, key=lambda item: item.requirement_id)
            ),
        )
    return ComponentLock(
        target_name="host",
        target_profile_identity=identity("invoice-cli-target"),
        selection_policy_identity=identity("selection-policy"),
        resolver_identity=identity("component-lock-resolver"),
        root_revision=nodes["invoice"].revision.identity,
        nodes=tuple(
            sorted(nodes.values(), key=lambda item: item.revision.identity.uri)
        ),
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
        _authorings=tuple(
            sorted(
                (
                    invoice_authoring,
                    pricing_authoring,
                    reporting_authoring,
                    money_authoring,
                ),
                key=lambda item: item.identity.uri,
            )
        ),
    )


def _diamond_lock_with_locked_money_asset() -> ComponentLock:
    """Put one ``ResolvedComponentAsset`` on the diamond money node.

    The existing diamond test passes an ``AuthoredBinaryAsset`` into
    ``plan_component_execution`` without placing assets on the lock. Sample
    ``plan`` callers omit that argument, so the lock itself must carry
    ``revision.assets``.
    """

    selector = ComponentAssetSelector(
        "currency-table",
        "assets/currency-table.bin",
        "assets/currency-table.bin",
        "currency-data",
    )
    resolved = ResolvedComponentAsset(selector, BlobRef("a" * 64, 12))
    lock = _diamond_lock()
    money = next(
        item for item in lock.nodes if item.revision.coordinate.name == "money"
    )
    old_money_uri = money.revision.identity.uri
    authoring = replace(money.revision.definition, assets=(selector,))
    revision = replace(
        money.revision,
        definition=authoring,
        authoring_identity=authoring.identity,
        assets=(resolved,),
    )
    new_money = node(revision, selected_flavor="python", capability="money-api")
    nodes = []
    for item in lock.nodes:
        if item.revision.coordinate.name == "money":
            nodes.append(new_money)
            continue
        satisfactions = tuple(
            replace(sat, provider_revision=revision.identity)
            if sat.provider_revision.uri == old_money_uri
            else sat
            for sat in item.requirement_constraint_satisfactions
        )
        nodes.append(replace(item, requirement_constraint_satisfactions=satisfactions))
    edges = tuple(
        replace(edge, provider_revision=revision.identity)
        if edge.provider_revision.uri == old_money_uri
        else edge
        for edge in lock.edges
    )
    authorings = tuple(
        authoring if item.coordinate.name == "money" else item
        for item in lock.authorings
    )
    return replace(
        lock,
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


def _models(lock: ComponentLock) -> dict[str, ContentIdentity]:
    return {
        item.revision.identity.uri: identity(
            f"model-{item.revision.coordinate.namespace}-{item.revision.coordinate.name}"
        )
        for item in lock.nodes
    }


def _keys_by_name(lock: ComponentLock) -> dict[str, ContentIdentity]:
    plan = plan_component_execution(lock, model_identities=_models(lock))
    names = {
        item.revision.identity.uri: item.revision.coordinate.name for item in lock.nodes
    }
    return {
        names[item.component_revision.uri]: item.generation_key.identity
        for item in plan.generation_plans
    }


class ComponentExecutionPlanningTests(unittest.TestCase):
    def test_diamond_is_stable_deduplicated_and_round_trips(self) -> None:
        lock = _diamond_lock()
        money_node = next(
            item for item in lock.nodes if item.revision.coordinate.name == "money"
        )
        asset = AuthoredBinaryAsset(
            money_node.revision.identity,
            "currency-table",
            "assets/currency-table.bin",
            "currency-data",
            lock.target_profile_identity,
            BlobRef("a" * 64, 12),
            identity("currency-table-authorization"),
        )
        plan = plan_component_execution(
            lock, model_identities=_models(lock), assets=(asset,)
        )
        self.assertEqual(plan, ComponentExecutionPlan.from_dict(plan.to_dict()))
        generation_action = next(
            item
            for item in plan.action_plans
            if item.phase is ComponentActionPhase.GENERATE
        )
        names = {
            item.revision.identity.uri: item.revision.coordinate.name
            for item in lock.nodes
        }
        named_layers = tuple(
            tuple(names[item.uri] for item in layer.component_revisions)
            for layer in generation_action.layers
        )
        self.assertEqual(named_layers[0], ("money",))
        self.assertEqual(set(named_layers[1]), {"pricing", "reporting"})
        self.assertEqual(named_layers[2], ("invoice-cli",))
        self.assertTrue(
            all(
                tuple(item.uri for item in layer.component_revisions)
                == tuple(sorted(item.uri for item in layer.component_revisions))
                for layer in generation_action.layers
            )
        )
        self.assertEqual(
            sum(name == "money" for layer in named_layers for name in layer), 1
        )
        money_plan = next(
            item
            for item in plan.generation_plans
            if names[item.component_revision.uri] == "money"
        )
        self.assertEqual(money_plan.generation_key.asset_identities, (asset.identity,))

    def test_authored_assets_from_lock_are_empty_without_locked_selectors(self) -> None:
        self.assertEqual(authored_assets_from_lock(_diamond_lock()), ())

    def test_authored_assets_from_lock_project_revision_assets(self) -> None:
        lock = _diamond_lock_with_locked_money_asset()
        bound = authored_assets_from_lock(lock)
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0].asset_id, "currency-table")
        money_plan = next(
            item
            for item in plan_component_execution(
                lock, model_identities=_models(lock), assets=bound
            ).generation_plans
            if item.component_revision == bound[0].component_revision
        )
        self.assertEqual(
            money_plan.generation_key.asset_identities, (bound[0].identity,)
        )

    def test_generation_keys_invalidate_only_local_and_direct_interface_consumers(
        self,
    ) -> None:
        baseline = _keys_by_name(_diamond_lock())
        private_leaf = _keys_by_name(_diamond_lock(money_spec="money-private-v2"))
        self.assertNotEqual(baseline["money"], private_leaf["money"])
        self.assertEqual(baseline["pricing"], private_leaf["pricing"])
        self.assertEqual(baseline["reporting"], private_leaf["reporting"])
        self.assertEqual(baseline["invoice-cli"], private_leaf["invoice-cli"])

        public_leaf = _keys_by_name(_diamond_lock(money_interface="money-interface-v2"))
        self.assertNotEqual(baseline["money"], public_leaf["money"])
        self.assertNotEqual(baseline["pricing"], public_leaf["pricing"])
        self.assertNotEqual(baseline["reporting"], public_leaf["reporting"])
        self.assertEqual(baseline["invoice-cli"], public_leaf["invoice-cli"])

        local_root = _keys_by_name(
            _diamond_lock(invoice_spec="invoice-specification-v2")
        )
        self.assertNotEqual(baseline["invoice-cli"], local_root["invoice-cli"])
        self.assertEqual(baseline["money"], local_root["money"])
        self.assertEqual(baseline["pricing"], local_root["pricing"])
        self.assertEqual(baseline["reporting"], local_root["reporting"])

    def test_missing_interface_and_cycles_fail_before_generation(self) -> None:
        missing = _diamond_lock()
        object.__setattr__(missing.edges[0], "public_interface_identity", None)
        with self.assertRaisesRegex(
            ComponentExecutionPlanningError, "has no public interface"
        ) as missing_error:
            plan_component_execution(missing, model_identities=_models(missing))
        self.assertEqual(
            missing_error.exception.code, "component_plan.interface_missing"
        )

        cyclic = _diamond_lock()
        invoice = next(
            item
            for item in cyclic.nodes
            if item.revision.coordinate.name == "invoice-cli"
        )
        money = next(
            item for item in cyclic.nodes if item.revision.coordinate.name == "money"
        )
        reverse = ExecutableComponentEdge(
            money.revision.identity,
            invoice.revision.identity,
            "cycle-test",
            "cycle-test-api",
            DependencyKind.BUILD,
            None,
        )
        object.__setattr__(
            cyclic,
            "edges",
            tuple(
                sorted(
                    (*cyclic.edges, reverse),
                    key=lambda item: (
                        item.consumer_revision.uri,
                        item.provider_revision.uri,
                        item.requirement_id,
                        item.kind.value,
                    ),
                )
            ),
        )
        with self.assertRaisesRegex(
            ComponentExecutionPlanningError, "cyclic"
        ) as cycle_error:
            plan_component_execution(cyclic, model_identities=_models(cyclic))
        self.assertEqual(cycle_error.exception.code, "component_plan.graph_cyclic")
        self.assertIn(" -> ", str(cycle_error.exception))


if __name__ == "__main__":
    unittest.main()
