"""Neutral descriptor, Component graph, and Flavor resolution tests."""

from __future__ import annotations

import unittest

from literate_ai.composition import (
    ComponentComposer,
    CompositionError,
    ExplicitFlavorSelectionPolicy,
    FlavorResolver,
    PreferredSelectionPolicy,
    UniqueSelectionPolicy,
    version_satisfies,
)
from literate_ai.contracts import (
    Capability,
    CapabilityConstraint,
    CapabilityRequirement,
    ComponentCoordinate,
    ComponentDefinition,
    ComponentRevision,
    ContentReference,
    ContributionKind,
    ContributionReference,
    DependencyKind,
    Entrypoint,
    FlavorAxis,
    FlavorCardinality,
    FlavorCoordinate,
    FlavorCoRequisiteGroup,
    FlavorDefinition,
    FlavorSlot,
    MergeOperator,
    SpecificationArtifact,
    SpecificationRequirement,
    SpecificationScenario,
    SpecificationSet,
    TargetConstraint,
    TargetProfile,
    canonical_identity,
)
from literate_ai.registry import (
    AvailabilityReason,
    AvailabilityStatus,
    ComponentDescriptor,
    DescriptorAttribute,
    DescriptorRegistry,
    FlavorDescriptor,
    MaterializationMetadata,
    MaterializationState,
)


def identity(label: str):
    return canonical_identity({"fixture": label})


def reference(kind: str, label: str) -> ContentReference:
    return ContentReference(kind, f"fixture://{label}", identity(label))


def requirement(
    requirement_id: str,
    capability: str,
    version_range: str = ">=1,<2",
    constraints: tuple[CapabilityConstraint, ...] = (),
    *,
    optional: bool = False,
) -> CapabilityRequirement:
    return CapabilityRequirement(
        requirement_id,
        capability,
        version_range,
        DependencyKind.RUNTIME,
        optional=optional,
        constraints=constraints,
    )


def definition(
    name: str,
    *,
    provides: tuple[Capability, ...] = (),
    requires: tuple[CapabilityRequirement, ...] = (),
    slots: tuple[FlavorSlot, ...] = (),
) -> ComponentDefinition:
    return ComponentDefinition(
        ComponentCoordinate("test", name),
        "1.0.0",
        name,
        "fixture",
        ("test",),
        False,
        provides,
        requires,
        "openspec",
        ("openspec",),
        (),
        reference("workflow", "workflow"),
        reference("routing-policy", "routing"),
        slots,
        (Entrypoint("run", "command", f"src/{name}.py"),),
        (),
    )


def descriptor(
    name: str,
    *,
    provides: tuple[Capability, ...] = (),
    requires: tuple[CapabilityRequirement, ...] = (),
    attributes: tuple[DescriptorAttribute, ...] = (),
    availability: AvailabilityStatus = AvailabilityStatus.AVAILABLE,
) -> ComponentDescriptor:
    reasons = (
        ()
        if availability is AvailabilityStatus.AVAILABLE
        else (AvailabilityReason("not-installed", "not available on this machine"),)
    )
    return ComponentDescriptor(
        identity(f"revision-{name}"),
        definition(name, provides=provides, requires=requires),
        availability,
        reasons,
        attributes,
    )


def specification_set() -> SpecificationSet:
    return SpecificationSet(
        "openspec",
        "1",
        (SpecificationArtifact("spec.md", identity("spec")),),
        (
            SpecificationRequirement(
                "behavior",
                "Behavior",
                "Behavior remains target-neutral.",
                (SpecificationScenario("works", "Works", (), ("used",), ("works",)),),
            ),
        ),
    )


class RegistryAndComponentCompositionTests(unittest.TestCase):
    def test_vocabulary_includes_unavailable_without_materializing_source(self) -> None:
        unavailable = descriptor(
            "remote",
            provides=(Capability("feature.remote", "1.0.0"),),
            availability=AvailabilityStatus.UNAVAILABLE,
        )
        registry = DescriptorRegistry((unavailable,))
        vocabulary = registry.vocabulary()
        self.assertEqual(vocabulary.components, (unavailable,))
        self.assertEqual(
            vocabulary.components[0].availability_reasons[0].code, "not-installed"
        )
        self.assertFalse(hasattr(registry, "materialize_source"))

        metadata = MaterializationMetadata(
            unavailable.revision_identity,
            MaterializationState.PARTIAL,
            (identity("source"), identity("index")),
            (identity("source"),),
        )
        self.assertEqual(metadata.missing_objects, (identity("index"),))
        self.assertEqual(
            unavailable.revision_identity, vocabulary.components[0].revision_identity
        )

    def test_semver_constraints_and_explicit_provider_policy(self) -> None:
        root = descriptor(
            "root",
            requires=(
                requirement(
                    "engine",
                    "engine.api",
                    ">=1.2,<2",
                    (CapabilityConstraint("platform.os", "in", ("linux",)),),
                ),
            ),
        )
        old = descriptor(
            "old",
            provides=(Capability("engine.api", "1.1.9"),),
            attributes=(DescriptorAttribute("platform.os", ("linux",)),),
        )
        mac = descriptor(
            "mac",
            provides=(Capability("engine.api", "1.5.0"),),
            attributes=(DescriptorAttribute("platform.os", ("macos",)),),
        )
        linux = descriptor(
            "linux",
            provides=(Capability("engine.api", "1.5.0"),),
            attributes=(DescriptorAttribute("platform.os", ("linux",)),),
        )
        composition = ComponentComposer(
            DescriptorRegistry((root, old, mac, linux)), UniqueSelectionPolicy()
        ).compose(root.revision_identity)
        self.assertEqual(composition.edges[0].target_revision, linux.revision_identity)
        reasons = [
            reason
            for item in composition.decisions[0].candidates
            for reason in item.reasons
        ]
        self.assertTrue(any("does not satisfy" in reason for reason in reasons))
        self.assertTrue(any("constraint" in reason for reason in reasons))
        self.assertTrue(version_satisfies("1.9.0", ">=1.2,<2"))

        second = descriptor(
            "linux-two",
            provides=(Capability("engine.api", "1.6.0"),),
            attributes=(DescriptorAttribute("platform.os", ("linux",)),),
        )
        ambiguous = DescriptorRegistry((root, linux, second))
        with self.assertRaisesRegex(CompositionError, "explicit policy") as raised:
            ComponentComposer(ambiguous).compose(root.revision_identity)
        self.assertEqual(raised.exception.code, "ambiguous-provider")
        selected = ComponentComposer(
            ambiguous, PreferredSelectionPolicy((second.coordinate,))
        ).compose(root.revision_identity)
        self.assertEqual(selected.edges[0].target_revision, second.revision_identity)

    def test_transitive_diamond_is_deduplicated_but_edges_are_preserved(self) -> None:
        leaf = descriptor("leaf", provides=(Capability("leaf.api", "1.0.0"),))
        left = descriptor(
            "left",
            provides=(Capability("left.api", "1.0.0"),),
            requires=(requirement("leaf", "leaf.api"),),
        )
        right = descriptor(
            "right",
            provides=(Capability("right.api", "1.0.0"),),
            requires=(requirement("leaf", "leaf.api"),),
        )
        root = descriptor(
            "diamond",
            requires=(
                requirement("left", "left.api"),
                requirement("right", "right.api"),
            ),
        )
        result = ComponentComposer(
            DescriptorRegistry((root, left, right, leaf))
        ).compose(root.revision_identity)
        self.assertEqual(len(result.revisions), 4)
        self.assertEqual(len(result.edges), 4)
        self.assertEqual(
            sum(
                edge.target_revision == leaf.revision_identity for edge in result.edges
            ),
            2,
        )
        self.assertTrue(
            all(edge.kind is DependencyKind.RUNTIME for edge in result.edges)
        )

    def test_absent_optional_requirement_is_recorded_without_an_edge(self) -> None:
        root = descriptor(
            "optional-root",
            requires=(requirement("telemetry", "telemetry.api", optional=True),),
        )
        result = ComponentComposer(DescriptorRegistry((root,))).compose(
            root.revision_identity
        )

        self.assertEqual(result.revisions, (root.revision_identity,))
        self.assertEqual(result.edges, ())
        self.assertEqual(len(result.decisions), 1)
        decision = result.decisions[0]
        self.assertTrue(decision.requirement.optional)
        self.assertIsNone(decision.selected_revision)
        self.assertIsNone(decision.selected_capability)
        self.assertEqual(decision.candidates, ())

    def test_optional_requirement_resolves_with_eligible_provider(self) -> None:
        root = descriptor(
            "optional-root",
            requires=(requirement("telemetry", "telemetry.api", optional=True),),
        )
        provider = descriptor(
            "telemetry", provides=(Capability("telemetry.api", "1.2.0"),)
        )
        result = ComponentComposer(DescriptorRegistry((root, provider))).compose(
            root.revision_identity
        )

        self.assertEqual(result.edges[0].target_revision, provider.revision_identity)
        self.assertEqual(
            result.decisions[0].selected_revision, provider.revision_identity
        )

    def test_optional_requirement_preserves_rejected_candidate_evidence(self) -> None:
        root = descriptor(
            "optional-root",
            requires=(requirement("telemetry", "telemetry.api", optional=True),),
        )
        unavailable = descriptor(
            "telemetry",
            provides=(Capability("telemetry.api", "1.2.0"),),
            availability=AvailabilityStatus.UNAVAILABLE,
        )
        result = ComponentComposer(DescriptorRegistry((root, unavailable))).compose(
            root.revision_identity
        )

        self.assertEqual(result.edges, ())
        self.assertEqual(len(result.decisions[0].candidates), 1)
        self.assertFalse(result.decisions[0].candidates[0].eligible)
        self.assertIsNone(result.decisions[0].selected_revision)

    def test_caret_ranges_respect_zero_major_compatibility(self) -> None:
        self.assertTrue(version_satisfies("1.9.0", "^1.2.3"))
        self.assertFalse(version_satisfies("2.0.0", "^1.2.3"))
        self.assertTrue(version_satisfies("0.2.9", "^0.2.3"))
        self.assertFalse(version_satisfies("0.3.0", "^0.2.3"))
        self.assertTrue(version_satisfies("0.0.3", "^0.0.3"))
        self.assertFalse(version_satisfies("0.0.4", "^0.0.3"))
        self.assertTrue(version_satisfies("0.9.0", "^0"))
        self.assertFalse(version_satisfies("1.0.0", "^0"))
        self.assertTrue(version_satisfies("0.0.9", "^0.0"))
        self.assertFalse(version_satisfies("0.1.0", "^0.0"))

    def test_cycle_fails_with_machine_code(self) -> None:
        first = descriptor(
            "first",
            provides=(Capability("first.api", "1.0.0"),),
            requires=(requirement("second", "second.api"),),
        )
        second = descriptor(
            "second",
            provides=(Capability("second.api", "1.0.0"),),
            requires=(requirement("first", "first.api"),),
        )
        with self.assertRaises(CompositionError) as raised:
            ComponentComposer(DescriptorRegistry((first, second))).compose(
                first.revision_identity
            )
        self.assertEqual(raised.exception.code, "dependency-cycle")


def flavor_descriptor(
    name: str,
    axis: FlavorAxis,
    axis_value: str,
    *,
    conflicts: tuple[str, ...] = (),
    co_requisites: tuple[str, ...] = (),
    order_before: tuple[str, ...] = (),
    contributions: tuple[ContributionReference, ...] = (),
    secondary_constraints: tuple[TargetConstraint, ...] = (),
    co_requisite_groups: tuple[FlavorCoRequisiteGroup, ...] = (),
) -> FlavorDescriptor:
    definition_value = FlavorDefinition(
        FlavorCoordinate("test", name),
        "1.0.0",
        name,
        axis,
        secondary_constraints,
        ("app.api",),
        (),
        (),
        (reference("specification", f"{name}-spec"),),
        (),
        contributions,
        conflicts,
        co_requisites,
        order_before,
        (),
        (axis_value,),
        co_requisite_groups,
    )
    return FlavorDescriptor(identity(f"flavor-{name}"), definition_value, axis_value)


class FlavorCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform_slot = FlavorSlot(
            "os", FlavorAxis.PLATFORM_OS, FlavorCardinality.EXACTLY_ONE, "platform"
        )
        self.language_slot = FlavorSlot(
            "language",
            FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
            FlavorCardinality.EXACTLY_ONE,
            "language",
        )
        self.base = ComponentRevision(
            definition(
                "app",
                provides=(Capability("app.api", "1.0.0"),),
                slots=(self.platform_slot, self.language_slot),
            ),
            specification_set(),
            None,
            (),
            (),
        )
        self.target = TargetProfile(
            "linux-rust",
            "1.0.0",
            "explicit",
            identity("target-provider"),
            (
                TargetConstraint(FlavorAxis.PLATFORM_OS, "linux"),
                TargetConstraint(FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM, "rust"),
            ),
        )

    def test_axis_cardinality_corequisite_order_and_effective_identity(self) -> None:
        rust = flavor_descriptor(
            "rust",
            FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
            "rust",
        )
        linux = flavor_descriptor(
            "linux",
            FlavorAxis.PLATFORM_OS,
            "linux",
            co_requisites=(rust.coordinate,),
            order_before=(rust.coordinate,),
        )
        result = FlavorResolver(DescriptorRegistry(flavors=(rust, linux))).resolve(
            self.base, self.target
        )
        self.assertEqual(
            result.lock.selected_revisions,
            (linux.revision_identity, rust.revision_identity),
        )
        self.assertEqual(result.lock.target_profile, self.target)
        self.assertEqual(
            result.effective_revision.flavor_set_lock, result.lock.identity
        )
        self.assertNotEqual(result.effective_revision.identity, self.base.identity)

    def test_portable_flavor_requires_exactly_one_host_realization(self) -> None:
        apple = flavor_descriptor("swift-apple", FlavorAxis.TOOLCHAIN, "swift-apple")
        linux_toolchain = flavor_descriptor(
            "swift-linux", FlavorAxis.TOOLCHAIN, "swift-linux"
        )
        swift = flavor_descriptor(
            "swift",
            FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
            "swift",
            co_requisite_groups=(
                FlavorCoRequisiteGroup(
                    "swift-host",
                    (apple.coordinate, linux_toolchain.coordinate),
                ),
            ),
        )

        with self.assertRaises(CompositionError) as missing:
            FlavorResolver._check_selected_relationships([swift])
        self.assertEqual(missing.exception.code, "flavor-corequisite-group-unsatisfied")

        FlavorResolver._check_selected_relationships([swift, apple])

        with self.assertRaises(CompositionError) as multiple:
            FlavorResolver._check_selected_relationships(
                [swift, apple, linux_toolchain]
            )
        self.assertEqual(
            multiple.exception.code, "flavor-corequisite-group-unsatisfied"
        )

    def test_slot_scoped_targets_select_two_roles_on_one_axis(self) -> None:
        backend = FlavorSlot(
            "backend-language",
            FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
            FlavorCardinality.EXACTLY_ONE,
            "language",
        )
        frontend = FlavorSlot(
            "frontend-language",
            FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
            FlavorCardinality.EXACTLY_ONE,
            "language",
        )
        base = ComponentRevision(
            definition(
                "full-stack-app",
                provides=(Capability("app.api", "1.0.0"),),
                slots=(backend, frontend),
            ),
            specification_set(),
            None,
            (),
            (),
        )
        target = TargetProfile(
            "rust-javascript-stack",
            "1.0.0",
            "explicit",
            identity("full-stack-target"),
            (
                TargetConstraint(
                    FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
                    "rust",
                    slot_id="backend-language",
                ),
                TargetConstraint(
                    FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
                    "javascript",
                    slot_id="frontend-language",
                ),
            ),
        )
        rust = flavor_descriptor(
            "rust", FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM, "rust"
        )
        javascript = flavor_descriptor(
            "javascript",
            FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
            "javascript",
        )

        result = FlavorResolver(DescriptorRegistry(flavors=(rust, javascript))).resolve(
            base, target
        )

        self.assertEqual(
            set(result.lock.selected_revisions),
            {rust.revision_identity, javascript.revision_identity},
        )
        self.assertEqual(
            TargetConstraint.from_dict(target.constraints[0].to_dict()),
            target.constraints[0],
        )

        axis_wide = TargetProfile(
            "ambiguous-stack",
            "1.0.0",
            "explicit",
            identity("ambiguous-stack-target"),
            (TargetConstraint(FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM, "rust"),),
        )
        with self.assertRaises(CompositionError) as ambiguous:
            FlavorResolver(DescriptorRegistry(flavors=(rust, javascript))).resolve(
                base, axis_wide
            )
        self.assertEqual(ambiguous.exception.code, "ambiguous-target-axis")

    def test_explicit_policy_reuses_one_revision_across_role_slots(self) -> None:
        slots = tuple(
            FlavorSlot(
                slot_id,
                FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
                FlavorCardinality.EXACTLY_ONE,
                "language",
            )
            for slot_id in ("backend-language", "worker-language")
        )
        base = ComponentRevision(
            definition(
                "rust-services",
                provides=(Capability("app.api", "1.0.0"),),
                slots=slots,
            ),
            specification_set(),
            None,
            (),
            (),
        )
        target = TargetProfile(
            "rust-services",
            "1.0.0",
            "explicit",
            identity("rust-services-target"),
            tuple(
                TargetConstraint(
                    FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
                    "rust",
                    slot_id=slot.slot_id,
                )
                for slot in slots
            ),
        )
        rust = flavor_descriptor(
            "rust", FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM, "rust"
        )
        rust_alternative = flavor_descriptor(
            "rust-alternative",
            FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
            "rust",
        )
        revision_id = rust.revision_identity.uri
        policy = ExplicitFlavorSelectionPolicy(
            (revision_id,),
            tuple((slot.slot_id, revision_id) for slot in slots),
        )

        result = FlavorResolver(
            DescriptorRegistry(flavors=(rust, rust_alternative)), policy
        ).resolve(base, target)

        self.assertEqual(result.lock.selected_revisions, (rust.revision_identity,))
        self.assertEqual(len(result.lock.selected_revisions), 1)
        self.assertEqual(result.lock.resolution_policy, policy.identity)

    def test_flavor_conflict_corequisite_and_ambiguity_fail_explicitly(self) -> None:
        rust = flavor_descriptor(
            "rust", FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM, "rust"
        )
        linux = flavor_descriptor(
            "linux",
            FlavorAxis.PLATFORM_OS,
            "linux",
            conflicts=(rust.coordinate,),
        )
        with self.assertRaises(CompositionError) as conflict:
            FlavorResolver(DescriptorRegistry(flavors=(linux, rust))).resolve(
                self.base, self.target
            )
        self.assertEqual(conflict.exception.code, "flavor-conflict")

        linux_missing = flavor_descriptor(
            "linux-missing",
            FlavorAxis.PLATFORM_OS,
            "linux",
            co_requisites=("flavor://test/missing",),
        )
        with self.assertRaises(CompositionError) as corequisite:
            FlavorResolver(DescriptorRegistry(flavors=(linux_missing, rust))).resolve(
                self.base, self.target
            )
        self.assertEqual(corequisite.exception.code, "missing-flavor-corequisite")

        linux_two = flavor_descriptor("linux-two", FlavorAxis.PLATFORM_OS, "linux")
        with self.assertRaises(CompositionError) as ambiguous:
            FlavorResolver(
                DescriptorRegistry(flavors=(linux, linux_two, rust))
            ).resolve(self.base, self.target)
        self.assertEqual(ambiguous.exception.code, "ambiguous-provider")

    def test_singleton_contribution_conflict_is_rejected(self) -> None:
        toolchain_a = ContributionReference(
            "linux-toolchain",
            ContributionKind.TOOLCHAIN,
            MergeOperator.EXACT_SINGLETON,
            "compiler",
            reference("toolchain-constraint", "clang"),
        )
        toolchain_b = ContributionReference(
            "rust-toolchain",
            ContributionKind.TOOLCHAIN,
            MergeOperator.EXACT_SINGLETON,
            "compiler",
            reference("toolchain-constraint", "rustc"),
        )
        linux = flavor_descriptor(
            "linux", FlavorAxis.PLATFORM_OS, "linux", contributions=(toolchain_a,)
        )
        rust = flavor_descriptor(
            "rust",
            FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
            "rust",
            contributions=(toolchain_b,),
        )
        with self.assertRaises(CompositionError) as raised:
            FlavorResolver(DescriptorRegistry(flavors=(linux, rust))).resolve(
                self.base, self.target
            )
        self.assertEqual(raised.exception.code, "flavor-contribution-conflict")

    def test_multi_value_cardinalities_accept_policy_selected_sets(self) -> None:
        class SelectAllPolicy:
            @property
            def identity(self):
                return identity("select-all-policy")

            def choose(self, candidates, *, subject):
                del subject
                return candidates

        target = TargetProfile(
            "linux-multi",
            "1.0.0",
            "explicit",
            identity("multi-target-provider"),
            (TargetConstraint(FlavorAxis.PLATFORM_OS, "linux"),),
        )
        linux_a = flavor_descriptor("linux-a", FlavorAxis.PLATFORM_OS, "linux")
        linux_b = flavor_descriptor("linux-b", FlavorAxis.PLATFORM_OS, "linux")
        registry = DescriptorRegistry(flavors=(linux_a, linux_b))

        one_or_more = ComponentRevision(
            definition(
                "multi-app",
                provides=(Capability("app.api", "1.0.0"),),
                slots=(
                    FlavorSlot(
                        "os",
                        FlavorAxis.PLATFORM_OS,
                        FlavorCardinality.ONE_OR_MORE,
                        "platform",
                    ),
                ),
            ),
            specification_set(),
            None,
            (),
            (),
        )
        selected = FlavorResolver(registry, SelectAllPolicy()).resolve(
            one_or_more, target
        )
        self.assertEqual(
            selected.lock.selected_revisions,
            (linux_a.revision_identity, linux_b.revision_identity),
        )

        bounded = ComponentRevision(
            definition(
                "bounded-app",
                provides=(Capability("app.api", "1.0.0"),),
                slots=(
                    FlavorSlot(
                        "os",
                        FlavorAxis.PLATFORM_OS,
                        FlavorCardinality.BOUNDED,
                        "platform",
                        2,
                        2,
                    ),
                ),
            ),
            specification_set(),
            None,
            (),
            (),
        )
        selected = FlavorResolver(registry, SelectAllPolicy()).resolve(bounded, target)
        self.assertEqual(len(selected.lock.selected_flavors), 2)

    def test_required_and_optional_target_constraints_differ(self) -> None:
        accelerator_slot = FlavorSlot(
            "accelerator",
            FlavorAxis.ACCELERATOR,
            FlavorCardinality.ZERO_OR_ONE,
            "accelerator",
        )
        base = ComponentRevision(
            definition(
                "accelerated-app",
                provides=(Capability("app.api", "1.0.0"),),
                slots=(accelerator_slot,),
            ),
            specification_set(),
            None,
            (),
            (),
        )
        required = TargetProfile(
            "cuda-required",
            "1.0.0",
            "explicit",
            identity("cuda-required-provider"),
            (TargetConstraint(FlavorAxis.ACCELERATOR, "cuda"),),
        )
        with self.assertRaises(CompositionError) as raised:
            FlavorResolver(DescriptorRegistry()).resolve(base, required)
        self.assertEqual(raised.exception.code, "flavor-target-unsatisfied")

        optional = TargetProfile(
            "cuda-optional",
            "1.0.0",
            "explicit",
            identity("cuda-optional-provider"),
            (TargetConstraint(FlavorAxis.ACCELERATOR, "cuda", optional=True),),
        )
        result = FlavorResolver(DescriptorRegistry()).resolve(base, optional)
        self.assertEqual(result.lock.selected_flavors, ())

    def test_optional_secondary_constraint_allows_an_unspecified_axis(self) -> None:
        base = ComponentRevision(
            definition(
                "platform-app",
                provides=(Capability("app.api", "1.0.0"),),
                slots=(self.platform_slot,),
            ),
            specification_set(),
            None,
            (),
            (),
        )
        target = TargetProfile(
            "linux",
            "1.0.0",
            "explicit",
            identity("linux-target-provider"),
            (TargetConstraint(FlavorAxis.PLATFORM_OS, "linux"),),
        )
        linux = flavor_descriptor(
            "linux-optional-arch",
            FlavorAxis.PLATFORM_OS,
            "linux",
            secondary_constraints=(
                TargetConstraint(
                    FlavorAxis.PLATFORM_ARCHITECTURE,
                    "arm64",
                    optional=True,
                ),
            ),
        )
        result = FlavorResolver(DescriptorRegistry(flavors=(linux,))).resolve(
            base, target
        )
        self.assertEqual(result.lock.selected_revisions, (linux.revision_identity,))

    def test_merge_operators_produce_a_normalized_contribution_set(self) -> None:
        shared_validator = reference("validator", "shared-validator")
        shared_runtime = reference("runtime", "shared-runtime")
        shared_toolchain = reference("toolchain-constraint", "shared-toolchain")
        linux_contributions = (
            ContributionReference(
                "linux-validator",
                ContributionKind.VALIDATOR,
                MergeOperator.ADDITIVE_SET,
                "validators",
                shared_validator,
            ),
            ContributionReference(
                "shared-runtime",
                ContributionKind.RUNTIME,
                MergeOperator.KEYED_UNION,
                "runtime-entries",
                shared_runtime,
            ),
            ContributionReference(
                "linux-toolchain",
                ContributionKind.TOOLCHAIN,
                MergeOperator.EXACT_SINGLETON,
                "compiler",
                shared_toolchain,
            ),
        )
        rust_contributions = (
            ContributionReference(
                "rust-validator",
                ContributionKind.VALIDATOR,
                MergeOperator.ADDITIVE_SET,
                "validators",
                shared_validator,
            ),
            ContributionReference(
                "shared-runtime",
                ContributionKind.RUNTIME,
                MergeOperator.KEYED_UNION,
                "runtime-entries",
                shared_runtime,
            ),
            ContributionReference(
                "rust-runtime",
                ContributionKind.RUNTIME,
                MergeOperator.KEYED_UNION,
                "runtime-entries",
                reference("runtime", "rust-runtime"),
            ),
            ContributionReference(
                "rust-toolchain",
                ContributionKind.TOOLCHAIN,
                MergeOperator.EXACT_SINGLETON,
                "compiler",
                shared_toolchain,
            ),
        )
        linux = flavor_descriptor(
            "linux",
            FlavorAxis.PLATFORM_OS,
            "linux",
            contributions=linux_contributions,
        )
        rust = flavor_descriptor(
            "rust",
            FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
            "rust",
            contributions=rust_contributions,
        )

        result = FlavorResolver(DescriptorRegistry(flavors=(linux, rust))).resolve(
            self.base, self.target
        )
        self.assertEqual(
            tuple(
                contribution.contribution_id
                for contribution in result.effective_revision.contributions
            ),
            (
                "linux-validator",
                "shared-runtime",
                "linux-toolchain",
                "rust-runtime",
            ),
        )

    def test_keyed_union_rejects_one_key_with_different_content(self) -> None:
        linux = flavor_descriptor(
            "linux",
            FlavorAxis.PLATFORM_OS,
            "linux",
            contributions=(
                ContributionReference(
                    "runtime",
                    ContributionKind.RUNTIME,
                    MergeOperator.KEYED_UNION,
                    "runtime-entries",
                    reference("runtime", "linux-runtime"),
                ),
            ),
        )
        rust = flavor_descriptor(
            "rust",
            FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
            "rust",
            contributions=(
                ContributionReference(
                    "runtime",
                    ContributionKind.RUNTIME,
                    MergeOperator.KEYED_UNION,
                    "runtime-entries",
                    reference("runtime", "rust-runtime"),
                ),
            ),
        )

        with self.assertRaises(CompositionError) as raised:
            FlavorResolver(DescriptorRegistry(flavors=(linux, rust))).resolve(
                self.base, self.target
            )
        self.assertEqual(raised.exception.code, "duplicate-contribution-id")

    def test_merge_operator_disagreement_for_one_slot_is_rejected(self) -> None:
        linux = flavor_descriptor(
            "linux",
            FlavorAxis.PLATFORM_OS,
            "linux",
            contributions=(
                ContributionReference(
                    "linux-runtime",
                    ContributionKind.RUNTIME,
                    MergeOperator.ADDITIVE_SET,
                    "runtime",
                    reference("runtime", "linux-runtime"),
                ),
            ),
        )
        rust = flavor_descriptor(
            "rust",
            FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
            "rust",
            contributions=(
                ContributionReference(
                    "rust-runtime",
                    ContributionKind.RUNTIME,
                    MergeOperator.KEYED_UNION,
                    "runtime",
                    reference("runtime", "rust-runtime"),
                ),
            ),
        )

        with self.assertRaises(CompositionError) as raised:
            FlavorResolver(DescriptorRegistry(flavors=(linux, rust))).resolve(
                self.base, self.target
            )
        self.assertEqual(raised.exception.code, "flavor-contribution-conflict")


if __name__ == "__main__":
    unittest.main()
