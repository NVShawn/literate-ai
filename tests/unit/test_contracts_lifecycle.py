"""Round-trip and invariant tests for the foundational lifecycle contracts."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from literate_ai.contracts import (
    AttestationFact,
    CandidateStatus,
    Capability,
    CapabilityConstraint,
    CapabilityRequirement,
    ComponentCoordinate,
    ComponentDefinition,
    ComponentRevision,
    ComponentRevisionRef,
    ContentReference,
    ContractValidationError,
    ContributionKind,
    ContributionReference,
    DependencyEdge,
    DependencyKind,
    EffectiveRevision,
    EffectiveSpecificationSet,
    Entrypoint,
    FlavorAxis,
    FlavorCandidateDecision,
    FlavorCardinality,
    FlavorCoordinate,
    FlavorDefinition,
    FlavorRevision,
    FlavorSetLock,
    FlavorSlot,
    MergeOperator,
    OriginAttestation,
    RevocationStatus,
    SourceEntry,
    SourceEntryType,
    SourceSnapshot,
    SpecificationArtifact,
    SpecificationRequirement,
    SpecificationScenario,
    SpecificationSet,
    TargetConstraint,
    TargetProfile,
    VerificationResult,
    VersionedContentRef,
    canonical_identity,
)


def identity(label: str):
    return canonical_identity({"fixture": label})


def reference(kind: str, label: str) -> ContentReference:
    return ContentReference(kind, f"fixture://{label}", identity(label))


class ContractLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capability = Capability("service.http.hello", "1.0.0")
        self.requirement = CapabilityRequirement(
            "hello-runtime",
            "service.http.hello",
            ">=1,<2",
            DependencyKind.RUNTIME,
            constraints=(
                CapabilityConstraint("platform.os", "in", ("linux", "macos")),
            ),
        )
        self.specifications = SpecificationSet(
            provider_kind="openspec",
            provider_version="1.7.0",
            artifacts=(
                SpecificationArtifact(
                    "openspec/specs/hello/spec.md", identity("spec-file")
                ),
            ),
            requirements=(
                SpecificationRequirement(
                    "hello",
                    "Say hello",
                    "The component shall return a greeting.",
                    (
                        SpecificationScenario(
                            "default",
                            "Default greeting",
                            (),
                            ("called",),
                            ("returns hello",),
                        ),
                    ),
                ),
            ),
            baseline_id="hello@1",
        )

    def component_definition(self) -> ComponentDefinition:
        return ComponentDefinition(
            coordinate=ComponentCoordinate("example", "hello"),
            version="1.0.0",
            display_name="Hello",
            description="Neutral greeting component",
            profiles=("library", "sample"),
            sample=True,
            provides=(self.capability,),
            requires=(),
            specification_provider="openspec",
            specification_roots=("openspec",),
            authoring_inputs=(reference("skill", "python-skill"),),
            workflow_definition=reference("workflow", "source-grounded"),
            routing_policy=reference("routing-policy", "standard"),
            flavor_slots=(
                FlavorSlot(
                    "language",
                    FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
                    FlavorCardinality.EXACTLY_ONE,
                    "capability://literate-ai/implementation-flavor@1",
                ),
            ),
            entrypoints=(Entrypoint("run", "command", "src/hello/main.py"),),
            acceptance_contracts=(reference("acceptance", "hello-contract"),),
        )

    def test_component_revision_and_specifications_round_trip(self) -> None:
        revision = ComponentRevision(
            self.component_definition(),
            self.specifications,
            None,
            (),
            (identity("run"),),
        )
        rebuilt = ComponentRevision.from_dict(revision.to_dict())
        self.assertEqual(rebuilt, revision)
        self.assertEqual(rebuilt.coordinate.uri, "component://example/hello")
        self.assertEqual(rebuilt.identity, revision.identity)

        changed = revision.to_dict()
        changed["generated_at"] = "operational data is forbidden"
        with self.assertRaisesRegex(ContractValidationError, "unknown fields"):
            ComponentRevision.from_dict(changed)

    def test_component_flavor_slots_allow_role_slots_on_the_same_axis(self) -> None:
        value = self.component_definition().to_dict()
        duplicate = dict(value["flavor_slots"][0])
        duplicate["slot_id"] = "second-language"
        value["flavor_slots"].append(duplicate)
        rebuilt = ComponentDefinition.from_dict(value)
        self.assertEqual(
            tuple(slot.slot_id for slot in rebuilt.flavor_slots),
            ("language", "second-language"),
        )

        duplicate["slot_id"] = "language"
        with self.assertRaisesRegex(ContractValidationError, "slot IDs"):
            ComponentDefinition.from_dict(value)

    def test_typed_dependency_edge_is_exact_and_explainable(self) -> None:
        edge = DependencyEdge(
            identity("consumer"),
            identity("provider"),
            self.requirement,
            self.capability,
            identity("resolution-decision"),
        )
        self.assertEqual(edge.kind, DependencyKind.RUNTIME)
        self.assertEqual(DependencyEdge.from_dict(edge.to_dict()), edge)
        with self.assertRaisesRegex(ContractValidationError, "satisfy"):
            DependencyEdge(
                identity("consumer"),
                identity("provider"),
                self.requirement,
                Capability("different", "1"),
                identity("decision"),
            )

    def test_source_tree_identity_and_attestation_are_independent(self) -> None:
        entries = (
            SourceEntry(
                "src/hello.py",
                SourceEntryType.FILE,
                0o644,
                12,
                identity("hello-source"),
            ),
        )
        tree = SourceSnapshot.compute_tree_identity(entries, ())
        attestation = OriginAttestation(
            signer="release@example.test",
            trust_root="fixture-root",
            verification_method="sigstore-bundle",
            tree_identity=tree,
            source_provider="git",
            source_facts=(AttestationFact("commit", "abc123"),),
            revocation_status=RevocationStatus.GOOD,
            result=VerificationResult.VERIFIED,
        )
        snapshot = SourceSnapshot(
            "git",
            identity("git-config"),
            "abc123",
            entries,
            (),
            tree,
            False,
            None,
            (attestation,),
        )
        self.assertEqual(SourceSnapshot.from_dict(snapshot.to_dict()), snapshot)
        self.assertNotEqual(snapshot.tree_identity, snapshot.identity)

        tampered = snapshot.to_dict()
        tampered["entries"][0]["size"] = 13
        with self.assertRaisesRegex(ContractValidationError, "canonical tree manifest"):
            SourceSnapshot.from_dict(tampered)

    def test_target_profile_can_express_an_unflavored_target(self) -> None:
        profile = TargetProfile(
            "portable-default",
            "1.0.0",
            "explicit",
            identity("target-provider"),
            (),
        )
        self.assertEqual(TargetProfile.from_dict(profile.to_dict()), profile)

    def test_flavor_lock_and_effective_revision_preserve_every_identity(self) -> None:
        profile = TargetProfile(
            "release-linux-rust",
            "1.0.0",
            "explicit",
            identity("target-provider"),
            (
                TargetConstraint(FlavorAxis.PLATFORM_OS, "linux"),
                TargetConstraint(FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM, "rust"),
            ),
        )
        contribution = ContributionReference(
            "rust-workflow",
            ContributionKind.WORKFLOW,
            MergeOperator.EXACT_SINGLETON,
            "generation-workflow",
            reference("workflow", "rust-workflow"),
        )
        definition = FlavorDefinition(
            FlavorCoordinate("example", "rust"),
            "1.0.0",
            "Rust",
            FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
            (),
            ("service.http.hello",),
            (),
            (),
            (reference("specification", "rust-spec"),),
            (reference("skill", "rust-skill"),),
            (contribution,),
            (),
            (),
            (),
            (),
            ("*-unknown-linux-*",),
        )
        multi_target = definition.to_dict()
        multi_target["supported_targets"].append("*-apple-darwin-*")
        with self.assertRaisesRegex(ContractValidationError, "exactly one"):
            FlavorDefinition.from_dict(multi_target)
        flavor_revision = FlavorRevision(definition, identity("flavor-source"), ())
        base_identity = identity("base-revision")
        base_ref = ComponentRevisionRef(
            ComponentCoordinate("example", "hello"), "1.0.0", base_identity
        )
        flavor_ref = VersionedContentRef(
            "flavor",
            flavor_revision.definition.coordinate.uri,
            flavor_revision.definition.version,
            flavor_revision.identity,
        )
        lock = FlavorSetLock(
            base_identity,
            profile,
            identity("resolver-policy"),
            (
                FlavorCandidateDecision(
                    flavor_revision.identity, CandidateStatus.SELECTED, ()
                ),
            ),
            (flavor_revision.identity,),
            base_ref,
            (flavor_ref,),
        )
        effective_specs = EffectiveSpecificationSet(
            self.specifications.identity, (identity("rust-spec"),), lock.identity
        )
        effective = EffectiveRevision(
            lock.base_revision,
            profile.identity,
            lock.identity,
            effective_specs.identity,
            (contribution,),
            lock.resolution_policy,
        )
        self.assertEqual(FlavorSetLock.from_dict(lock.to_dict()), lock)
        self.assertEqual(EffectiveRevision.from_dict(effective.to_dict()), effective)
        self.assertNotEqual(effective.identity, lock.base_revision)

        with self.assertRaisesRegex(ContractValidationError, "exactly match"):
            FlavorSetLock(
                lock.base_revision,
                profile,
                lock.resolution_policy,
                lock.candidates,
                (),
                base_ref,
                (),
            )


class JsonSchemaTests(unittest.TestCase):
    def test_schema_documents_are_strict_valid_json_with_resolvable_urns(self) -> None:
        root = Path(__file__).parents[2] / "schemas" / "v1"
        documents = [
            json.loads(path.read_text()) for path in sorted(root.glob("*.schema.json"))
        ]
        catalog = json.loads((root / "index.json").read_text())
        expected_files = {item["file"] for item in catalog["schemas"]}
        actual_files = {path.name for path in root.glob("*.schema.json")}
        self.assertEqual(actual_files, expected_files)
        self.assertEqual(len(documents), len(expected_files))

        ids: set[str] = set()
        refs: set[str] = set()

        def walk(value):
            if isinstance(value, dict):
                if "$id" in value:
                    ids.add(value["$id"])
                if "$ref" in value:
                    refs.add(value["$ref"].split("#", 1)[0])
                if value.get("type") == "object":
                    additional = value.get("additionalProperties")
                    self.assertTrue(
                        additional is False or isinstance(additional, dict),
                        "object schemas must close fields or type their map values",
                    )
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        for document in documents:
            self.assertEqual(
                document["$schema"], "https://json-schema.org/draft/2020-12/schema"
            )
            walk(document)
        self.assertTrue(
            refs - {""} <= ids, f"unresolved schema resources: {refs - ids}"
        )


if __name__ == "__main__":
    unittest.main()
