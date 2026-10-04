"""Focused stdlib tests for local lifecycle infrastructure."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.contracts import (
    ComponentCoordinate,
    ComponentRevisionRef,
    canonical_identity,
)
from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.channel_events import CHANNEL_MARKER, ChannelEvent
from literate_ai.publication import (
    FilesystemPublicationTarget,
    ImportAuthorization,
    ImportPolicy,
    ImportRequest,
    PublicationError,
    PublicationManifest,
    PublicationPolicy,
    PublicationRequest,
    PublicationService,
)
from literate_ai.security import SecurityProfile
from literate_ai.settings import (
    SecretReference,
    SettingDefinition,
    SettingScope,
    SettingsDocument,
    SettingsError,
    SettingsFileStore,
    SettingsRegistry,
    UnknownSettingError,
)
from literate_ai.storage import (
    AppendOnlyEventStore,
    BlobIntegrityError,
    FileSystemCAS,
    StorageSafetyError,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
PUBLICATION_POLICY_DIGEST = canonical_identity({"policy": "publication-test"}).uri


def settings_registry() -> SettingsRegistry:
    return SettingsRegistry(
        [
            SettingDefinition("models.timeout", int, 30),
            SettingDefinition("models.endpoint", str, "http://localhost"),
            SettingDefinition(
                "models.credential",
                SecretReference,
                SecretReference(provider="env", name="MODEL_API_KEY"),
                allowed_scopes=frozenset(
                    {SettingScope.USER, SettingScope.MACHINE, SettingScope.RUN}
                ),
            ),
        ]
    )


class LayeredSettingsTests(unittest.TestCase):
    def test_settings_persistence_rejects_raw_secrets_unknowns_and_symlinks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = settings_registry()
            store = SettingsFileStore(root / "settings", registry)
            user = SettingsDocument(
                schema_version=1,
                scope=SettingScope.USER,
                values={
                    "models.credential": SecretReference(
                        provider="env", name="MODEL_API_KEY"
                    )
                },
            )
            path = store.save(user)
            self.assertNotIn("actual-secret", path.read_text(encoding="utf-8"))
            self.assertEqual(
                store.load(SettingScope.USER).values["models.credential"],
                SecretReference(provider="env", name="MODEL_API_KEY"),
            )

            with self.assertRaises(SettingsError):
                registry.validate_document(
                    SettingsDocument(
                        schema_version=1,
                        scope=SettingScope.USER,
                        values={"models.credential": "actual-secret"},
                    )
                )
            # Moved from the deleted test_user_config: secret-shaped channel
            # event fields are rejected rather than persisted.
            with self.assertRaises(ContractValidationError):
                ChannelEvent.from_dict(
                    {
                        "schema": ChannelEvent.SCHEMA,
                        "marker": CHANNEL_MARKER,
                        "role": "author",
                        "kind": "mutagenic-cli",
                        "project_id": "demo",
                        "user": "xoxb-1234567890-token",
                    }
                )
            with self.assertRaises(UnknownSettingError):
                registry.validate_document(
                    SettingsDocument(
                        schema_version=1,
                        scope=SettingScope.USER,
                        values={"unknown.value": 1},
                    )
                )

            path.unlink()
            outside = root / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            path.symlink_to(outside)
            with self.assertRaises(StorageSafetyError):
                store.save(user)


class PublicationTests(unittest.TestCase):
    def _manifest(
        self,
        cas: FileSystemCAS,
        target: FilesystemPublicationTarget,
        *,
        security_profile: SecurityProfile = SecurityProfile.CONSTRAINED,
    ) -> tuple[PublicationManifest, PublicationPolicy, bytes, bytes]:
        source = b"print('hello')\n"
        provenance = b'{"generator":"deterministic"}\n'
        source_ref = cas.put_bytes(source, media_type="text/x-python")
        provenance_ref = cas.put_bytes(provenance, media_type="application/json")
        component_ref = ComponentRevisionRef(
            ComponentCoordinate("test", "demo-component"),
            "1.0.0",
            canonical_identity({"component": "demo-component", "version": "1.0.0"}),
        )
        policy = PublicationPolicy(
            PUBLICATION_POLICY_DIGEST,
            allowed_targets=(target.target_id,),
        )
        request = PublicationRequest.create(
            component_ref=component_ref,
            component_lock_identity=canonical_identity(
                {"component-lock": "demo-component@1.0.0"}
            ),
            effective_revision_digest=canonical_identity(
                {"effective": "demo-component@1.0.0"}
            ).uri,
            source_bundle=source_ref,
            roots={"source": source_ref, "provenance": provenance_ref},
            blobs=[source_ref, provenance_ref],
            provenance=(provenance_ref,),
            security_classification_digest=canonical_identity(
                {"classification": "demo-component@1.0.0"}
            ).uri,
            security_profile=security_profile,
            target_id=target.target_id,
            target_identity_digest=target.identity,
            policy_digest=policy.policy_digest,
            actor="release-manager",
        )
        authorization = policy.authorize(
            request,
            reason="publish verified fixture",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        manifest = PublicationManifest.create(
            request=request,
            authorization=authorization,
            created_at=NOW,
        )
        return manifest, policy, source, provenance

    def _import_context(
        self,
        service: PublicationService,
        manifest: PublicationManifest,
        manifest_ref,
        target: FilesystemPublicationTarget,
    ) -> tuple[ImportPolicy, ImportRequest, ImportAuthorization]:
        policy = ImportPolicy(
            policy_digest=canonical_identity({"policy": "local-import-test"}).uri,
            allowed_source_targets=(target.target_id,),
            allowed_classification_digests=(
                manifest.request.security_classification_digest,
            ),
            trusted_publication_policy_digests=(manifest.request.policy_digest,),
        )
        request = ImportRequest.create(
            manifest=manifest,
            publication_manifest=manifest_ref,
            destination_identity_digest=service.import_destination_identity,
            policy_digest=policy.policy_digest,
            actor="local-importer",
        )
        authorization = policy.authorize(
            request,
            reason="import verified publication fixture",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        return policy, request, authorization

    def test_publication_rejects_overlap_symlinks_and_tampered_blobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas = FileSystemCAS(root / "cas")
            events = AppendOnlyEventStore(root / "events")

            overlapping = FilesystemPublicationTarget("overlap", cas.root / "remote")
            overlap_manifest, overlap_policy, _, _ = self._manifest(cas, overlapping)
            with self.assertRaises(StorageSafetyError):
                PublicationService(
                    cas,
                    events,
                    overlap_policy,
                    clock=lambda: NOW + timedelta(minutes=1),
                ).publish(overlap_manifest, overlapping)

            target = FilesystemPublicationTarget("safe", root / "published")
            manifest, policy, _, _ = self._manifest(cas, target)
            receipt = PublicationService(
                cas,
                events,
                policy,
                clock=lambda: NOW + timedelta(minutes=1),
            ).publish(manifest, target)
            target.path_for(manifest.roots["source"]).write_bytes(b"tampered")
            destination = FileSystemCAS(root / "destination")
            destination_events = AppendOnlyEventStore(root / "destination-events")
            importer = PublicationService(
                destination,
                destination_events,
                clock=lambda: NOW + timedelta(minutes=1),
            )
            import_policy, _, import_authorization = self._import_context(
                importer,
                manifest,
                receipt.publication_manifest,
                target,
            )
            importer.import_policy = import_policy
            with self.assertRaises(BlobIntegrityError):
                importer.import_release(
                    receipt.publication_manifest,
                    target,
                    expected_component=manifest.component_ref,
                    import_authorization=import_authorization,
                )

            symlink_root = root / "target-link"
            symlink_root.symlink_to(root / "published", target_is_directory=True)
            with self.assertRaises(StorageSafetyError):
                FilesystemPublicationTarget("linked", symlink_root)

    def test_policy_rejects_yolo_provenance_without_explicit_permission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas = FileSystemCAS(root / "cas")
            target = FilesystemPublicationTarget("release", root / "published")
            with self.assertRaisesRegex(PublicationError, "profile"):
                self._manifest(cas, target, security_profile=SecurityProfile.YOLO)


if __name__ == "__main__":
    unittest.main()
