"""Neutral descriptor, Component graph, and Flavor resolution tests."""

from __future__ import annotations

import unittest

from literate_ai.composition import (
    ComponentComposer,
    CompositionError,
    FlavorResolver,
)
from literate_ai.contracts import (
    Capability,
    CapabilityRequirement,
    ComponentCoordinate,
    ComponentDefinition,
    ComponentRevision,
    ContentReference,
    DependencyKind,
    Entrypoint,
    FlavorAxis,
    FlavorCardinality,
    FlavorCoordinate,
    FlavorDefinition,
    FlavorSlot,
    SpecificationArtifact,
    SpecificationRequirement,
    SpecificationScenario,
    SpecificationSet,
    TargetConstraint,
    TargetProfile,
    canonical_identity,
)
from literate_ai.registry import (
    AvailabilityStatus,
    ComponentDescriptor,
    DescriptorRegistry,
    FlavorDescriptor,
)


def identity(label: str):
    return canonical_identity({"fixture": label})


def reference(kind: str, label: str) -> ContentReference:
    return ContentReference(kind, f"fixture://{label}", identity(label))


def requirement(requirement_id: str, capability: str) -> CapabilityRequirement:
    return CapabilityRequirement(
        requirement_id, capability, ">=1,<2", DependencyKind.RUNTIME
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
) -> ComponentDescriptor:
    return ComponentDescriptor(
        identity(f"revision-{name}"),
        definition(name, provides=provides, requires=requires),
        AvailabilityStatus.AVAILABLE,
        (),
        (),
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
) -> FlavorDescriptor:
    definition_value = FlavorDefinition(
        FlavorCoordinate("test", name),
        "1.0.0",
        name,
        axis,
        (),
        ("app.api",),
        (),
        (),
        (reference("specification", f"{name}-spec"),),
        (),
        (),
        conflicts,
        co_requisites,
        (),
        (),
        (axis_value,),
        (),
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


if __name__ == "__main__":
    unittest.main()
