"""Cross-domain exact-version coexistence and compatibility gates."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.artifacts import BundleKind, BundleManifest
from literate_ai.contracts import (
    ComponentCoordinate,
    ComponentRevisionRef,
    FlavorAxis,
    MigrationError,
    SchemaCompatibilityReader,
    SchemaMigrationRegistry,
    SemanticVersion,
    TargetConstraint,
    TargetProfile,
    VersionedContentRef,
    legacy_ambiguity,
)
from literate_ai.models import (
    DataEgress,
    Locality,
    ModelEndpoint,
    ModelGroup,
    ModelRouter,
    RoutingError,
    StageModelPolicy,
)
from literate_ai.publication import (
    FilesystemPublicationTarget,
    ImportPolicy,
    ImportRequest,
    PublicationError,
    PublicationManifest,
    PublicationPolicy,
    PublicationRequest,
    PublicationService,
)
from literate_ai.registry import (
    ComponentDescriptor,
    DescriptorRegistry,
    RegistryAmbiguityError,
)
from literate_ai.security import SecurityProfile
from literate_ai.source_to_specification import (
    SkillCatalog,
    SourceToSpecificationError,
    SpecAuthoringSkill,
    SpecAuthoringSkillSet,
    resolve_skill_set,
)
from literate_ai.storage import AppendOnlyEventStore, FileSystemCAS

try:  # discovery from the repository root keeps the package context
    from .test_registry_composition import definition, identity
except ImportError:  # discovery rooted at tests/unit loads modules as top level
    from test_registry_composition import definition, identity


def component_ref(name: str, version: str, label: str) -> ComponentRevisionRef:
    return ComponentRevisionRef(
        ComponentCoordinate("test", name), version, identity(label)
    )


def endpoint(version: str, *, model: str) -> ModelEndpoint:
    return ModelEndpoint(
        endpoint_id="generator",
        provider="local",
        model=model,
        base_url="http://127.0.0.1:8000",
        locality=Locality.LOCAL,
        capabilities=("structured",),
        context_tokens=4096,
        version=version,
    )


def skill(version: str, digest: str) -> SpecAuthoringSkill:
    return SpecAuthoringSkill(
        skill_id="author",
        version=version,
        content_digest=digest,
        title="Author",
        capabilities=("author",),
        facets=("source",),
        evidence_kinds=("symbols",),
    )


class SemanticVersionTests(unittest.TestCase):
    def test_domain_semver_and_distribution_pep440_are_separate(self) -> None:
        self.assertLess(
            SemanticVersion.parse("1.0.0-alpha.1"),
            SemanticVersion.parse("1.0.0"),
        )
        self.assertEqual(str(SemanticVersion.parse("2.3.4+build.7")), "2.3.4+build.7")
        for invalid in ("1", "1.2", "01.2.3", "0.1.1a1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                SemanticVersion.parse(invalid)

    def test_exact_refs_round_trip_without_losing_any_identity_dimension(self) -> None:
        reference = component_ref("demo", "2.1.0-rc.1", "demo-r2")
        self.assertEqual(ComponentRevisionRef.from_dict(reference.to_dict()), reference)
        self.assertIn("@2.1.0-rc.1#sha256:", reference.uri)
        generic = reference.versioned_content
        self.assertEqual(VersionedContentRef.from_dict(generic.to_dict()), generic)


class CoexistenceTests(unittest.TestCase):
    def test_registry_and_composer_keep_two_component_versions(self) -> None:
        first_definition = definition("shared")
        second_definition = replace(first_definition, version="2.0.0")
        first = ComponentDescriptor(identity("shared-r1"), first_definition)
        second = ComponentDescriptor(identity("shared-r2"), second_definition)
        registry = DescriptorRegistry((first, second))

        revisions = registry.component_revisions(first_definition.coordinate)
        self.assertEqual(len(revisions), 2)
        self.assertEqual(registry.component_exact(second.ref), second)
        with self.assertRaises(RegistryAmbiguityError):
            registry.resolve_component(first_definition.coordinate)

        from literate_ai.composition import ComponentComposer

        composition = ComponentComposer(registry).compose(second.ref)
        self.assertEqual(composition.root_ref, second.ref)
        self.assertEqual(composition.revision_refs, (second.ref,))

    def test_model_revisions_coexist_but_legacy_id_is_ambiguous(self) -> None:
        first_endpoint = endpoint("1.0.0", model="small")
        second_endpoint = endpoint("2.0.0", model="large")
        first_group = ModelGroup(
            "generation", "1.0.0", ("generator",), (first_endpoint.ref,)
        )
        second_group = ModelGroup(
            "generation", "2.0.0", ("generator",), (second_endpoint.ref,)
        )
        router = ModelRouter(
            endpoints=(first_endpoint, second_endpoint),
            groups=(first_group, second_group),
        )
        legacy = StageModelPolicy(
            "generate", "generation", "generation", data_egress=DataEgress.NONE
        )
        with self.assertRaises(RoutingError) as caught:
            router.select(legacy)
        self.assertEqual(caught.exception.code, "models.legacy_group_ambiguous")

        exact = replace(legacy, group_ref=second_group.ref)
        decision = router.select(exact)
        self.assertEqual(decision.group_ref, second_group.ref)
        self.assertEqual(decision.selected_endpoint_ref, second_endpoint.ref)

    def test_skill_catalog_resolves_exact_ref_and_rejects_legacy_id(self) -> None:
        first = skill("1.0.0", "sha256:skill-one")
        second = skill("2.0.0", "sha256:skill-two")
        catalog = SkillCatalog()
        catalog.add(first)
        catalog.add(second)
        with self.assertRaises(SourceToSpecificationError) as caught:
            _ = catalog["author"]
        self.assertEqual(caught.exception.code, "skill.legacy_id_ambiguous")
        selected = SpecAuthoringSkillSet("selected", "1.0.0", (second.ref,))
        self.assertEqual(resolve_skill_set(selected, catalog), (second,))


class CompatibilityTests(unittest.TestCase):
    def test_reader_rejects_future_and_ambiguous_legacy_documents(self) -> None:
        migrations = SchemaMigrationRegistry()

        def migrate(document: dict[str, object]) -> dict[str, object]:
            if "version" not in document:
                raise legacy_ambiguity("legacy item has no version")
            return {**document, "schema": "example/item@2"}

        migrations.register("example/item@1", "example/item@2", migrate)
        reader = SchemaCompatibilityReader(
            current_schema="example/item@2",
            parser=dict,
            migrations=migrations,
            legacy_schemas=frozenset({"example/item@1"}),
        )
        with self.assertRaises(MigrationError) as ambiguous:
            reader.read({"schema": "example/item@1", "name": "item"})
        self.assertEqual(
            ambiguous.exception.code, "contracts.migration_legacy_ambiguous"
        )
        with self.assertRaises(MigrationError) as future:
            reader.read({"schema": "example/item@3", "name": "item"})
        self.assertEqual(future.exception.code, "contracts.migration_future_schema")

    def test_legacy_profile_and_bundle_require_explicit_identity_context(self) -> None:
        profile = TargetProfile(
            "linux",
            "1.0.0",
            "explicit",
            identity("profile-provider"),
            (TargetConstraint(FlavorAxis.PLATFORM_OS, "linux"),),
        )
        current = profile.to_dict()
        legacy_profile = dict(current)
        legacy_profile.pop("version")
        with self.assertRaises(MigrationError):
            TargetProfile.from_legacy_dict(legacy_profile)
        migrated_profile = TargetProfile.from_legacy_dict(
            legacy_profile, version="1.0.0"
        )
        self.assertEqual(migrated_profile.version, "1.0.0")

        with tempfile.TemporaryDirectory() as directory:
            cas = FileSystemCAS(Path(directory))
            root = cas.put_bytes(b"source")
            reference = component_ref("bundle", "1.0.0", "bundle-r1")
            manifest = BundleManifest(
                BundleKind.SOURCE,
                reference.revision_identity.uri,
                reference.revision_identity.uri,
                None,
                {"source": root},
                (),
                (),
                component_ref=reference,
            )
            legacy_bundle = manifest.to_dict()
            legacy_bundle["schema_version"] = 1
            legacy_bundle.pop("component_ref")
            with self.assertRaises(MigrationError) as ambiguous_bundle:
                BundleManifest.from_dict(legacy_bundle)
            self.assertEqual(
                ambiguous_bundle.exception.code,
                "contracts.migration_legacy_ambiguous",
            )
            self.assertEqual(
                BundleManifest.from_dict(
                    legacy_bundle, legacy_component_ref=reference
                ).component_ref,
                reference,
            )

    def test_publication_import_rejects_an_unexpected_component_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_cas = FileSystemCAS(root / "source")
            blob = source_cas.put_bytes(b"source")
            provenance = source_cas.put_bytes(
                b'{"provenance":"test"}', media_type="application/json"
            )
            first = component_ref("published", "1.0.0", "published-r1")
            second = component_ref("published", "2.0.0", "published-r2")
            target = FilesystemPublicationTarget("target", root / "published")
            policy = PublicationPolicy(
                identity("publication-policy").uri,
                allowed_targets=("target",),
            )
            request = PublicationRequest.create(
                component_ref=first,
                component_lock_identity=identity("published-component-lock"),
                effective_revision_digest=identity("published-effective").uri,
                source_bundle=blob,
                roots={"source": blob, "provenance": provenance},
                blobs=(blob, provenance),
                provenance=(provenance,),
                security_classification_digest=identity("published-classification").uri,
                security_profile=SecurityProfile.CONSTRAINED,
                target_id="target",
                target_identity_digest=target.identity,
                policy_digest=policy.policy_digest,
                actor="publisher",
            )
            now = datetime(2026, 8, 3, tzinfo=UTC)
            authorization = policy.authorize(
                request,
                reason="versioned publication test",
                issued_at=now,
                expires_at=now + timedelta(minutes=5),
            )
            manifest = PublicationManifest.create(
                request=request,
                authorization=authorization,
                created_at=now,
            )
            published = PublicationService(
                source_cas,
                AppendOnlyEventStore(root / "publish-events"),
                policy,
                clock=lambda: now + timedelta(minutes=1),
            ).publish(manifest, target)
            importer = PublicationService(
                FileSystemCAS(root / "destination"),
                AppendOnlyEventStore(root / "import-events"),
            )
            import_policy = ImportPolicy(
                identity("local-import-policy").uri,
                allowed_source_targets=(target.target_id,),
                allowed_classification_digests=(
                    manifest.request.security_classification_digest,
                ),
                trusted_publication_policy_digests=(manifest.request.policy_digest,),
            )
            import_request = ImportRequest.create(
                manifest=manifest,
                publication_manifest=published.publication_manifest,
                destination_identity_digest=importer.import_destination_identity,
                policy_digest=import_policy.policy_digest,
                actor="importer",
            )
            import_authorization = import_policy.authorize(
                import_request,
                reason="version mismatch test",
                issued_at=now,
                expires_at=now + timedelta(minutes=5),
            )
            importer.import_policy = import_policy
            with self.assertRaisesRegex(PublicationError, "expected Component"):
                importer.import_release(
                    published.publication_manifest,
                    target,
                    expected_component=second,
                    import_authorization=import_authorization,
                )

            legacy = {
                "schema_version": 2,
                "component_id": manifest.component_id,
                "revision": manifest.revision,
                "component_ref": manifest.component_ref.to_dict(),
                "roots": {
                    key: value.to_dict() for key, value in manifest.roots.items()
                },
                "blobs": [item.to_dict() for item in manifest.blobs],
                "created_at": manifest.created_at,
            }
            with self.assertRaises(MigrationError) as ambiguous:
                PublicationManifest.from_dict(legacy)
            self.assertEqual(
                ambiguous.exception.code,
                "contracts.migration_legacy_ambiguous",
            )
            self.assertEqual(
                PublicationManifest.from_dict(
                    legacy,
                    legacy_request=request,
                    legacy_authorization=authorization,
                ),
                manifest,
            )


if __name__ == "__main__":
    unittest.main()
