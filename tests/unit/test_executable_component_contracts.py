"""Executable Component boundary and exact diamond-invalidation tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from jsonschema import Draft202012Validator

from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.contracts.executable_components import (
    EXECUTABLE_EDGE_SEMANTICS,
    AssetAssemblyMode,
    AuthoredBinaryAsset,
    CandidateReplacementPolicy,
    CompatibilityPolicy,
    CompatibilityPromise,
    ComponentActionPhase,
    ComponentChangeSurface,
    ComponentInterfaceBinding,
    ComponentInvalidationDecision,
    ComponentInvalidationTable,
    DependencyInputKind,
    ExecutableComponentEdge,
    ExportedProtocol,
    ExportedType,
    IntentionalInterfaceReExport,
    InterfaceError,
    LifecycleDriverTrust,
    LifecycleDriverTrustBinding,
    NodeFlavorSlotResolution,
    NodeTargetFlavorSelection,
    PublicInterfaceContract,
    SelectedNodeFlavor,
)
from literate_ai.contracts.flavors import FlavorAxis, FlavorCardinality, FlavorSlot
from literate_ai.contracts.identity import ContentIdentity, canonical_identity

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads(
    (ROOT / "schemas/v2/executable-components.schema.json").read_text(encoding="utf-8")
)
VALIDATOR = Draft202012Validator(SCHEMA)
ASSERTIONS = unittest.TestCase()


def identity(label: str) -> ContentIdentity:
    return canonical_identity({"fixture": label})


def ordered(*values: ContentIdentity) -> tuple[ContentIdentity, ...]:
    return tuple(sorted(values, key=lambda item: item.uri))


def public_interface(
    *,
    provider: str = "pricing",
    re_exports: tuple[IntentionalInterfaceReExport, ...] = (),
) -> PublicInterfaceContract:
    return PublicInterfaceContract(
        capability=f"{provider}-api",
        version="1.2.0",
        exported_types=(
            ExportedType("quote", "A decimal amount and ISO-4217 currency code."),
        ),
        exported_protocols=(
            ExportedProtocol(
                "price-service",
                ("price(sku, quantity) -> quote",),
            ),
        ),
        preconditions=("quantity is a positive integer",),
        postconditions=("the returned quote uses the requested quantity",),
        errors=(
            InterfaceError(
                "unknown-sku",
                "the SKU is absent",
                "the caller may select another SKU",
            ),
        ),
        compatibility=CompatibilityPromise(
            CompatibilityPolicy.SEMVER_STABLE,
            ">=1.2.0,<2.0.0",
            "Minor revisions preserve accepted requests and typed outcomes.",
        ),
        re_exports=re_exports,
    )


def diamond_table() -> ComponentInvalidationTable:
    invoice = identity("invoice-cli")
    pricing = identity("pricing")
    reporting = identity("reporting")
    money = identity("money")
    all_nodes = ordered(invoice, pricing, reporting, money)

    def decision(
        case_id: str,
        changed: ContentIdentity,
        surface: ComponentChangeSurface,
        regenerate: tuple[ContentIdentity, ...],
        rebuild: tuple[ContentIdentity, ...],
    ) -> ComponentInvalidationDecision:
        return ComponentInvalidationDecision(
            case_id,
            changed,
            surface,
            ordered(*regenerate),
            ordered(*rebuild),
            ordered(*rebuild),
        )

    return ComponentInvalidationTable(
        component_graph_identity=identity("invoice-diamond-graph"),
        component_revisions=all_nodes,
        decisions=(
            decision(
                "invoice-local",
                invoice,
                ComponentChangeSurface.LOCAL_AUTHORITY,
                (invoice,),
                (invoice,),
            ),
            decision(
                "money-asset",
                money,
                ComponentChangeSurface.AUTHORED_ASSET,
                (),
                all_nodes,
            ),
            decision(
                "money-local",
                money,
                ComponentChangeSurface.LOCAL_AUTHORITY,
                (money,),
                all_nodes,
            ),
            decision(
                "money-public-interface",
                money,
                ComponentChangeSurface.PUBLIC_INTERFACE,
                all_nodes,
                all_nodes,
            ),
            decision(
                "money-target-flavor",
                money,
                ComponentChangeSurface.TARGET_OR_FLAVOR,
                (money,),
                all_nodes,
            ),
            decision(
                "pricing-local",
                pricing,
                ComponentChangeSurface.LOCAL_AUTHORITY,
                (pricing,),
                (invoice, pricing),
            ),
            decision(
                "reporting-local",
                reporting,
                ComponentChangeSurface.LOCAL_AUTHORITY,
                (reporting,),
                (invoice, reporting),
            ),
        ),
    )


def _check_executable_component_schema_is_valid_draft_2020_12() -> None:
    Draft202012Validator.check_schema(SCHEMA)


def _check_public_interface_is_versioned_strict_and_identity_bearing() -> None:
    money = public_interface(provider="money")
    re_export = IntentionalInterfaceReExport(
        "money-api",
        "1.2.0",
        money.identity,
        ("money",),
    )
    contract = public_interface(re_exports=(re_export,))
    old_binding = ComponentInterfaceBinding(
        identity("pricing-private-revision-one"),
        contract.capability,
        contract.identity,
    )
    new_binding = replace(
        old_binding,
        component_revision=identity("pricing-private-revision-two"),
    )

    assert contract == PublicInterfaceContract.from_dict(contract.to_dict())
    VALIDATOR.validate(contract.to_dict())
    assert "provider_revision" not in contract.to_dict()
    assert old_binding == ComponentInterfaceBinding.from_dict(old_binding.to_dict())
    VALIDATOR.validate(old_binding.to_dict())
    VALIDATOR.validate(new_binding.to_dict())
    assert old_binding.identity != new_binding.identity
    assert old_binding.interface_identity == new_binding.interface_identity
    assert replace(contract, version="1.3.0").identity != contract.identity

    unknown = contract.to_dict()
    unknown["implementation_prompt"] = "copy the provider's private source"
    with ASSERTIONS.assertRaisesRegex(ContractValidationError, "unknown fields"):
        PublicInterfaceContract.from_dict(unknown)
    with ASSERTIONS.assertRaisesRegex(
        ContractValidationError, "must export at least one"
    ):
        replace(
            contract,
            exported_types=(),
            exported_protocols=(),
            re_exports=(),
        )
    with ASSERTIONS.assertRaisesRegex(ContractValidationError, "cannot shadow"):
        replace(contract, re_exports=(replace(re_export, symbols=("quote",)),))


def _check_every_dependency_kind_has_one_exact_executable_meaning() -> None:
    assert set(EXECUTABLE_EDGE_SEMANTICS) == set(DependencyKind)
    assert EXECUTABLE_EDGE_SEMANTICS[DependencyKind.GENERATION].consumed_input is (
        DependencyInputKind.PUBLIC_INTERFACE
    )
    assert EXECUTABLE_EDGE_SEMANTICS[DependencyKind.GENERATION].provider_phase is None
    assert {
        kind
        for kind, semantics in EXECUTABLE_EDGE_SEMANTICS.items()
        if semantics.visible_to_generation
    } == {DependencyKind.GENERATION}
    assert EXECUTABLE_EDGE_SEMANTICS[DependencyKind.RUNTIME].consumer_phase is (
        ComponentActionPhase.RUN
    )

    for kind in DependencyKind:
        edge = ExecutableComponentEdge(
            identity("consumer"),
            identity("provider"),
            f"needs-{kind.value}",
            "money-api",
            kind,
            identity("money-public-interface")
            if kind is DependencyKind.GENERATION
            else None,
        )
        assert edge == ExecutableComponentEdge.from_dict(edge.to_dict())
        VALIDATOR.validate(edge.to_dict())

    with ASSERTIONS.assertRaisesRegex(
        ContractValidationError, "must be a ContentIdentity"
    ):
        ExecutableComponentEdge(
            identity("consumer"),
            identity("provider"),
            "needs-generation",
            "money-api",
            DependencyKind.GENERATION,
            None,
        )
    with ASSERTIONS.assertRaisesRegex(ContractValidationError, "separate edge kinds"):
        ExecutableComponentEdge(
            identity("consumer"),
            identity("provider"),
            "needs-runtime",
            "money-api",
            DependencyKind.RUNTIME,
            identity("money-public-interface"),
        )


def _check_target_and_flavor_resolution_is_exact_and_per_node() -> None:
    build_slot = FlavorSlot(
        "build-system",
        FlavorAxis.BUILD_SYSTEM,
        FlavorCardinality.EXACTLY_ONE,
        "build-system",
    )
    language_slot = FlavorSlot(
        "language",
        FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
        FlavorCardinality.EXACTLY_ONE,
        "language",
    )
    os_slot = FlavorSlot(
        "operating-system",
        FlavorAxis.PLATFORM_OS,
        FlavorCardinality.EXACTLY_ONE,
        "operating-system",
    )
    selection = NodeTargetFlavorSelection(
        identity("pricing"),
        "host-cli",
        identity("pricing-target-profile"),
        identity("selection-policy"),
        (
            NodeFlavorSlotResolution(
                build_slot,
                (SelectedNodeFlavor("bazel", identity("bazel-flavor")),),
            ),
            NodeFlavorSlotResolution(
                language_slot,
                (SelectedNodeFlavor("rust", identity("rust-flavor")),),
            ),
            NodeFlavorSlotResolution(
                os_slot,
                (SelectedNodeFlavor("linux", identity("linux-flavor")),),
            ),
        ),
    )
    another_node = replace(
        selection,
        component_revision=identity("reporting"),
        target_profile_identity=identity("reporting-target-profile"),
        slots=(
            selection.slots[0],
            replace(
                selection.slots[1],
                selected=(SelectedNodeFlavor("python", identity("python-flavor")),),
            ),
            selection.slots[2],
        ),
    )

    assert selection == NodeTargetFlavorSelection.from_dict(selection.to_dict())
    VALIDATOR.validate(selection.to_dict())
    VALIDATOR.validate(another_node.to_dict())
    assert selection.identity != another_node.identity
    assert selection.selected_flavor_revisions == ordered(
        *(item.flavor_revision for slot in selection.slots for item in slot.selected)
    )
    selection.require_slots((build_slot, language_slot, os_slot))

    with ASSERTIONS.assertRaisesRegex(
        ContractValidationError, "every and only declared"
    ):
        replace(selection, slots=selection.slots[:-1]).require_slots(
            (build_slot, language_slot, os_slot)
        )
    with ASSERTIONS.assertRaisesRegex(ContractValidationError, "declared slot once"):
        replace(selection, slots=tuple(reversed(selection.slots)))

    optional = NodeFlavorSlotResolution(
        FlavorSlot(
            "accelerator",
            FlavorAxis.ACCELERATOR,
            FlavorCardinality.ZERO_OR_ONE,
            "accelerator",
        ),
        (),
    )
    many = NodeFlavorSlotResolution(
        FlavorSlot(
            "sanitizers",
            FlavorAxis.TOOLCHAIN,
            FlavorCardinality.ONE_OR_MORE,
            "sanitizer",
        ),
        (
            SelectedNodeFlavor("asan", identity("asan-flavor")),
            SelectedNodeFlavor("ubsan", identity("ubsan-flavor")),
        ),
    )
    bounded = NodeFlavorSlotResolution(
        FlavorSlot(
            "packages",
            FlavorAxis.PACKAGING,
            FlavorCardinality.BOUNDED,
            "package",
            1,
            3,
        ),
        (
            SelectedNodeFlavor("archive", identity("archive-flavor")),
            SelectedNodeFlavor("container", identity("container-flavor")),
        ),
    )
    cardinality_selection = replace(
        selection,
        slots=(optional, bounded, many),
    )
    cardinality_selection.require_slots((optional.slot, bounded.slot, many.slot))
    assert (
        NodeTargetFlavorSelection.from_dict(cardinality_selection.to_dict())
        == cardinality_selection
    )
    VALIDATOR.validate(cardinality_selection.to_dict())
    with ASSERTIONS.assertRaisesRegex(ContractValidationError, "exactly-one"):
        NodeFlavorSlotResolution(build_slot, ())
    with ASSERTIONS.assertRaisesRegex(ContractValidationError, "zero-or-one"):
        replace(optional, selected=many.selected)
    with ASSERTIONS.assertRaisesRegex(ContractValidationError, "requires 1..3"):
        replace(bounded, selected=())


def _check_assets_repair_and_lifecycle_driver_trust_are_fail_closed() -> None:
    asset = AuthoredBinaryAsset(
        identity("invoice-cli"),
        "brand-mark",
        "assets/brand.png",
        "brand-artwork",
        identity("invoice-target"),
        BlobRef("a" * 64, 128, media_type="image/png"),
        identity("asset-authorization"),
    )
    replacement = CandidateReplacementPolicy(maximum_repairs=2)
    standard = LifecycleDriverTrustBinding(
        LifecycleDriverTrust.STANDARD,
        identity("standard-driver"),
        identity("driver-policy"),
        identity("framework-distribution"),
        None,
    )
    external = LifecycleDriverTrustBinding(
        LifecycleDriverTrust.EXTERNAL,
        identity("external-driver"),
        identity("driver-policy"),
        None,
        identity("project-authorization"),
    )

    for value, parser in (
        (asset, AuthoredBinaryAsset.from_dict),
        (replacement, CandidateReplacementPolicy.from_dict),
        (standard, LifecycleDriverTrustBinding.from_dict),
        (external, LifecycleDriverTrustBinding.from_dict),
    ):
        assert value == parser(value.to_dict())
        VALIDATOR.validate(value.to_dict())

    with ASSERTIONS.assertRaisesRegex(
        ContractValidationError, "normalized and relative"
    ):
        replace(asset, path="assets/../private-key")
    with ASSERTIONS.assertRaisesRegex(
        ContractValidationError, "cannot be model-writable"
    ):
        replace(asset, model_writable=True)
    with ASSERTIONS.assertRaisesRegex(ContractValidationError, "between 0 and 2"):
        CandidateReplacementPolicy(maximum_repairs=3)
    with ASSERTIONS.assertRaisesRegex(ContractValidationError, "project_authorization"):
        replace(standard, project_authorization_identity=identity("project"))
    with ASSERTIONS.assertRaisesRegex(
        ContractValidationError, "framework_distribution"
    ):
        replace(external, framework_distribution_identity=identity("framework"))
    assert replacement.authored_assets is AssetAssemblyMode.AUTHORED_IMMUTABLE_OVERLAY


def _check_exact_diamond_invalidation_table_is_round_trip_and_schema_valid() -> None:
    table = diamond_table()
    by_case = {decision.case_id: decision for decision in table.decisions}
    names = {
        label: identity(label)
        for label in ("invoice-cli", "pricing", "reporting", "money")
    }
    all_nodes = ordered(*names.values())
    money_interface = public_interface(provider="money")
    money_private_before = ComponentInterfaceBinding(
        identity("money-private-before"), "money-api", money_interface.identity
    )
    money_private_after = replace(
        money_private_before,
        component_revision=identity("money-private-after"),
    )
    pricing_interface = public_interface(
        re_exports=(
            IntentionalInterfaceReExport(
                "money-api",
                "1.2.0",
                money_interface.identity,
                ("money",),
            ),
        )
    )
    changed_money_interface = replace(
        money_interface,
        postconditions=("the returned amount is rounded using bankers rounding",),
    )
    changed_pricing_interface = replace(
        pricing_interface,
        re_exports=(
            replace(
                pricing_interface.re_exports[0],
                interface_identity=changed_money_interface.identity,
            ),
        ),
    )

    assert money_private_before.interface_identity == (
        money_private_after.interface_identity
    )
    assert money_private_before.identity != money_private_after.identity
    assert money_interface.identity != changed_money_interface.identity
    assert pricing_interface.identity != changed_pricing_interface.identity
    assert by_case["invoice-local"].regenerate == (names["invoice-cli"],)
    assert by_case["pricing-local"].regenerate == (names["pricing"],)
    assert by_case["pricing-local"].rebuild == ordered(
        names["invoice-cli"], names["pricing"]
    )
    assert by_case["reporting-local"].rebuild == ordered(
        names["invoice-cli"], names["reporting"]
    )
    assert by_case["money-local"].regenerate == (names["money"],)
    assert by_case["money-local"].rebuild == all_nodes
    assert by_case["money-public-interface"].regenerate == all_nodes
    assert by_case["money-target-flavor"].regenerate == (names["money"],)
    assert by_case["money-asset"].regenerate == ()
    assert by_case["money-asset"].rebuild == all_nodes
    assert all(decision.rebuild == decision.retest for decision in table.decisions)

    assert table == ComponentInvalidationTable.from_dict(table.to_dict())
    VALIDATOR.validate(table.to_dict())

    escaped = replace(
        table.decisions[0],
        rebuild=ordered(names["invoice-cli"], identity("outside-graph")),
        retest=ordered(names["invoice-cli"], identity("outside-graph")),
    )
    with ASSERTIONS.assertRaisesRegex(
        ContractValidationError, "outside the exact graph"
    ):
        replace(table, decisions=(escaped, *table.decisions[1:]))
    with ASSERTIONS.assertRaisesRegex(
        ContractValidationError, "canonical identity order"
    ):
        replace(
            table.decisions[3],
            regenerate=tuple(reversed(table.decisions[3].regenerate)),
        )


class ExecutableComponentContractTests(unittest.TestCase):
    def test_schema_is_valid_draft_2020_12(self) -> None:
        _check_executable_component_schema_is_valid_draft_2020_12()

    def test_public_interface_is_strict_and_identity_bearing(self) -> None:
        _check_public_interface_is_versioned_strict_and_identity_bearing()

    def test_every_dependency_kind_has_one_executable_meaning(self) -> None:
        _check_every_dependency_kind_has_one_exact_executable_meaning()

    def test_target_and_flavor_resolution_is_exact_per_node(self) -> None:
        _check_target_and_flavor_resolution_is_exact_and_per_node()

    def test_assets_repair_and_driver_trust_are_fail_closed(self) -> None:
        _check_assets_repair_and_lifecycle_driver_trust_are_fail_closed()

    def test_exact_diamond_invalidation_table(self) -> None:
        _check_exact_diamond_invalidation_table_is_round_trip_and_schema_valid()
