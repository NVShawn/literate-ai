"""Focused stdlib tests for local lifecycle infrastructure."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from literate_ai.contracts import (
    ComponentCoordinate,
    ComponentRevisionRef,
    canonical_identity,
)
from literate_ai.publication import (
    FilesystemPublicationTarget,
    ImportAuthorization,
    ImportPolicy,
    ImportRequest,
    PublicationAuthorization,
    PublicationError,
    PublicationManifest,
    PublicationPolicy,
    PublicationRequest,
    PublicationService,
    TransferReceipt,
    validate_transfer_receipt_for_release,
)
from literate_ai.security import SecurityProfile
from literate_ai.settings import (
    SecretReference,
    SettingDefinition,
    SettingScope,
    SettingsDocument,
    SettingsError,
    SettingsFileStore,
    SettingsMigrator,
    SettingsRegistry,
    UnknownSettingError,
)
from literate_ai.storage import (
    AppendOnlyEventStore,
    BlobIntegrityError,
    BlobRef,
    DependencyEdge,
    DependencyIndex,
    EventStoreError,
    FileSystemCAS,
    IndexError,
    ReferenceConflictError,
    ReferenceIndex,
    StorageSafetyError,
)
from literate_ai.storage import events as event_storage

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
PUBLICATION_POLICY_DIGEST = canonical_identity({"policy": "publication-test"}).uri


class ContentAddressedStorageTests(unittest.TestCase):
    def test_blobs_and_manifests_are_immutable_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = FileSystemCAS(Path(directory) / "cas")
            first = cas.put_bytes(b"source", media_type="text/plain")
            duplicate = cas.put_bytes(b"source", media_type="text/plain")
            manifest = cas.put_manifest({"source": first.to_dict()})

            self.assertEqual(first, duplicate)
            self.assertEqual(cas.get_bytes(first), b"source")
            self.assertEqual(cas.get_manifest(manifest)["source"], first.to_dict())
            self.assertEqual(len(tuple(cas.iter_refs())), 2)

            cas.path_for(first).write_bytes(b"tampered")
            with self.assertRaises(BlobIntegrityError):
                cas.verify(first)
            with self.assertRaises(BlobIntegrityError):
                cas.put_bytes(b"source", media_type="text/plain")

    def test_cas_rejects_symbolic_roots_inputs_and_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual = root / "actual"
            actual.mkdir()
            root_link = root / "cas-link"
            root_link.symlink_to(actual, target_is_directory=True)
            with self.assertRaises(StorageSafetyError):
                FileSystemCAS(root_link)

            cas = FileSystemCAS(root / "cas")
            source = root / "source"
            source.write_text("secret", encoding="utf-8")
            source_link = root / "source-link"
            source_link.symlink_to(source)
            with self.assertRaises(StorageSafetyError):
                cas.put_file(source_link)

            reference = cas.put_bytes(b"prefix")
            prefix = cas.blob_root / reference.digest[:2]
            reference_path = cas.path_for(reference)
            reference_path.unlink()
            prefix.rmdir()
            outside = root / "outside"
            outside.mkdir()
            prefix.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(StorageSafetyError):
                cas.put_bytes(b"prefix")


class AppendOnlyEventTests(unittest.TestCase):
    def test_file_lock_persists_one_placeholder_byte_across_repeated_acquisitions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "events" / ".events.lock"
            # Read only after releasing the lock: Windows mandatory byte-range
            # locking applies even to a second handle opened by the same
            # process, so reading path bytes while the lock is still held
            # would race the same way a second acquirer's write would.
            with event_storage.FileLock(path):
                pass
            self.assertEqual(path.read_bytes(), b"\0")
            with event_storage.FileLock(path):
                pass
            self.assertEqual(path.read_bytes(), b"\0")

    def test_file_lock_serializes_concurrent_threads(self) -> None:
        import threading
        from concurrent.futures import ThreadPoolExecutor

        with tempfile.TemporaryDirectory() as directory:
            directory = str(Path(directory).resolve())
            path = Path(directory) / "events" / ".events.lock"
            counter_path = Path(directory) / "counter"
            counter_path.write_text("0", encoding="utf-8")
            workers = 16
            barrier = threading.Barrier(workers)

            def increment() -> None:
                barrier.wait(timeout=10)
                with event_storage.FileLock(path):
                    value = int(counter_path.read_text(encoding="utf-8"))
                    counter_path.write_text(str(value + 1), encoding="utf-8")

            with ThreadPoolExecutor(max_workers=workers) as executor:
                list(executor.map(lambda _: increment(), range(workers)))

            self.assertEqual(int(counter_path.read_text(encoding="utf-8")), workers)

    def test_file_lock_rejects_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "target.lock"
            target.write_bytes(b"")
            link = root / "events" / ".events.lock"
            link.parent.mkdir()
            link.symlink_to(target)
            with self.assertRaises(StorageSafetyError):
                with event_storage.FileLock(link):
                    pass

    def test_events_are_sequenced_and_hash_chained_per_stream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AppendOnlyEventStore(Path(directory) / "events")
            first = store.append("component:one", "created", {"revision": "1"})
            store.append("component:two", "created", {"revision": "a"})
            second = store.append("component:one", "accepted", {"revision": "2"})

            one = store.read("component:one")
            self.assertEqual([item.sequence for item in one], [1, 2])
            self.assertEqual(second.previous_digest, first.digest)
            self.assertEqual(store.read("component:one", after_sequence=1), (second,))
            self.assertEqual(store.streams(), ("component:one", "component:two"))

            lines = store.path.read_text(encoding="utf-8").splitlines()
            tampered = json.loads(lines[-1])
            tampered["data"]["revision"] = "forged"
            lines[-1] = json.dumps(tampered, sort_keys=True)
            store.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(EventStoreError):
                store.read()


class IndexTests(unittest.TestCase):
    def test_references_are_compare_and_swap_aliases_to_verified_blobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas = FileSystemCAS(root / "cas")
            index = ReferenceIndex(root / "indexes", cas)
            first = cas.put_bytes(b"one")
            second = cas.put_bytes(b"two")

            record = index.set("components", "demo", first, expected_generation=0)
            self.assertEqual(record.generation, 1)
            self.assertEqual(index.resolve("components", "demo").target, first)
            with self.assertRaises(ReferenceConflictError):
                index.set("components", "demo", second, expected_generation=0)

            updated = index.set(
                "components", "demo", second, expected_generation=record.generation
            )
            self.assertEqual(updated.generation, 2)
            self.assertEqual(index.resolve("components", "demo").target, second)
            self.assertEqual(cas.get_bytes(first), b"one")

    def test_dependency_forward_reverse_and_closure_are_separate_projections(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas = FileSystemCAS(root / "cas")
            dependency_index = DependencyIndex(root / "indexes", cas=cas)
            base_artifact = cas.put_bytes(b"base")
            middle_artifact = cas.put_bytes(b"middle")
            optional_artifact = cas.put_bytes(b"optional")

            dependency_index.replace(
                "middle",
                [
                    DependencyEdge(
                        subject_id="middle",
                        dependency_id="base",
                        kind="runtime",
                        artifact=base_artifact,
                    )
                ],
            )
            dependency_index.replace(
                "application",
                [
                    DependencyEdge(
                        subject_id="application",
                        dependency_id="middle",
                        kind="direct",
                        artifact=middle_artifact,
                    ),
                    DependencyEdge(
                        subject_id="application",
                        dependency_id="optional-tool",
                        kind="optional",
                        required=False,
                        artifact=optional_artifact,
                    ),
                ],
            )

            self.assertEqual(
                dependency_index.closure("application"), ("base", "middle")
            )
            self.assertEqual(
                dependency_index.closure("application", include_optional=True),
                ("base", "middle", "optional-tool"),
            )
            self.assertEqual(
                dependency_index.dependents("base")[0].subject_id, "middle"
            )
            with self.assertRaises(IndexError):
                dependency_index.replace(
                    "broken",
                    [
                        DependencyEdge(
                            subject_id="broken",
                            dependency_id="missing",
                            kind="build",
                            artifact=None,
                        )
                    ],
                )


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
    def test_resolution_retains_every_contribution_and_never_secret_values(
        self,
    ) -> None:
        registry = settings_registry()
        documents = [
            SettingsDocument(
                schema_version=1,
                scope=SettingScope.USER,
                values={
                    "models.timeout": 60,
                    "models.credential": SecretReference(
                        provider="env", name="TEAM_MODEL_KEY"
                    ),
                },
            ),
            SettingsDocument(
                schema_version=1,
                scope=SettingScope.RUN,
                values={"models.timeout": 5},
            ),
        ]

        snapshot = registry.resolve(documents)
        timeout = snapshot.settings["models.timeout"]
        self.assertEqual(timeout.value, 5)
        self.assertEqual(timeout.effective_scope, SettingScope.RUN)
        self.assertEqual(
            [item.scope for item in timeout.history],
            [
                SettingScope.FRAMEWORK_DEFAULT,
                SettingScope.USER,
                SettingScope.RUN,
            ],
        )
        secret = snapshot.value("models.credential")
        self.assertIsInstance(secret, SecretReference)
        self.assertTrue(secret.available({"TEAM_MODEL_KEY": "present"}))
        rendered = json.dumps(snapshot.to_dict(), sort_keys=True)
        self.assertIn("TEAM_MODEL_KEY", rendered)
        self.assertNotIn("present", rendered)

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

    def test_pure_versioned_migration_is_required(self) -> None:
        migrator = SettingsMigrator(current_version=2)

        def migrate_v1(document: dict[str, object]) -> dict[str, object]:
            values = dict(document["values"])  # type: ignore[arg-type]
            values["models.timeout"] = values.pop("models.old-timeout")
            return {**document, "schema_version": 2, "values": values}

        migrator.register(1, migrate_v1)
        registry = SettingsRegistry(
            [SettingDefinition("models.timeout", int, 30)], migrator=migrator
        )
        migrated = registry.validate_document(
            SettingsDocument(
                schema_version=1,
                scope=SettingScope.USER,
                values={"models.old-timeout": 45},
            )
        )
        self.assertEqual(migrated.schema_version, 2)
        self.assertEqual(migrated.values, {"models.timeout": 45})


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

    def test_publish_and_import_are_explicit_verified_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_cas = FileSystemCAS(root / "source-cas")
            source_events = AppendOnlyEventStore(root / "source-events")
            target = FilesystemPublicationTarget("team-cache", root / "published")
            manifest, policy, source, provenance = self._manifest(source_cas, target)
            publication = PublicationService(
                source_cas,
                source_events,
                policy,
                clock=lambda: NOW + timedelta(minutes=1),
            )

            receipt = publication.publish(manifest, target)
            self.assertIs(
                validate_transfer_receipt_for_release(manifest.request, receipt),
                receipt,
            )
            with self.assertRaisesRegex(ValueError, "differs"):
                validate_transfer_receipt_for_release(
                    manifest.request,
                    replace(receipt, target_id="another-target"),
                )
            repeated = publication.publish(manifest, target)
            self.assertEqual(receipt, repeated)
            self.assertEqual(receipt.direction, "publish")
            self.assertEqual(receipt.blob_count, 3)
            self.assertEqual(receipt.transferred_count, 3)
            self.assertEqual(
                receipt.effective_revision_digest,
                manifest.request.effective_revision_digest,
            )
            self.assertEqual(
                receipt.publication_request_digest,
                manifest.request.digest,
            )
            self.assertEqual(
                receipt.component_lock_identity,
                manifest.component_lock_identity,
            )
            self.assertEqual(
                receipt.publication_authorization_id,
                manifest.authorization.authorization_id,
            )
            self.assertEqual(TransferReceipt.from_dict(receipt.to_dict()), receipt)
            manifest_ref = receipt.publication_manifest
            self.assertEqual(
                target.publication_manifest_ref(
                    manifest.component_id, manifest_ref.digest
                ),
                manifest_ref,
            )

            destination_cas = FileSystemCAS(root / "destination-cas")
            destination_events = AppendOnlyEventStore(root / "destination-events")
            importer = PublicationService(
                destination_cas,
                destination_events,
                clock=lambda: NOW + timedelta(minutes=1),
            )
            import_policy, import_request, import_authorization = self._import_context(
                importer, manifest, manifest_ref, target
            )
            importer.import_policy = import_policy
            imported = importer.import_release(
                manifest_ref,
                target,
                expected_component=manifest.component_ref,
                import_authorization=import_authorization,
            )
            self.assertEqual(imported.direction, "import")
            self.assertEqual(imported.transferred_count, 3)
            self.assertEqual(imported.import_request_digest, import_request.digest)
            self.assertEqual(
                imported.component_lock_identity,
                import_request.component_lock_identity,
            )
            self.assertEqual(
                imported.import_authorization_id,
                import_authorization.authorization_id,
            )
            self.assertEqual(
                destination_cas.get_bytes(manifest.roots["source"]), source
            )
            self.assertEqual(
                destination_cas.get_bytes(manifest.roots["provenance"]), provenance
            )
            self.assertFalse((root / "published").samefile(root / "source-cas"))

    def test_interrupted_publication_resumes_from_verified_remote_blobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas = FileSystemCAS(root / "cas")
            events = AppendOnlyEventStore(root / "events")
            target = FilesystemPublicationTarget("resume-cache", root / "published")
            manifest, policy, _, _ = self._manifest(cas, target)
            service = PublicationService(
                cas,
                events,
                policy,
                clock=lambda: NOW + timedelta(minutes=1),
            )
            original = target.put_blob
            call_count = 0

            secret = "forwarded-publication-secret"

            def fail_after_one(source: FileSystemCAS, reference):
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise OSError(f"simulated interruption: {secret}")
                return original(source, reference)

            with mock.patch.object(target, "put_blob", side_effect=fail_after_one):
                with self.assertRaises(OSError):
                    service.publish(manifest, target)

            receipt = service.publish(manifest, target)
            operation_events = events.read(f"publication:{receipt.operation_id}")
            self.assertIn(
                "transfer.interrupted", [item.event_type for item in operation_events]
            )
            interrupted = next(
                item
                for item in operation_events
                if item.event_type == "transfer.interrupted"
            )
            self.assertEqual(interrupted.data, {"error_type": "OSError"})
            self.assertNotIn(secret, events.path.read_text(encoding="utf-8"))
            self.assertEqual(receipt.transferred_count, receipt.blob_count)
            self.assertEqual(
                len(
                    {
                        item.data["blob"]["digest"]
                        for item in operation_events
                        if item.event_type == "blob.transferred"
                    }
                ),
                receipt.blob_count,
            )

            destination_cas = FileSystemCAS(root / "destination-cas")
            destination_events = AppendOnlyEventStore(root / "destination-events")
            importer = PublicationService(
                destination_cas,
                destination_events,
                clock=lambda: NOW + timedelta(minutes=1),
            )
            import_policy, _, import_authorization = self._import_context(
                importer, manifest, receipt.publication_manifest, target
            )
            importer.import_policy = import_policy
            with mock.patch.object(
                target,
                "import_blob",
                side_effect=OSError(f"import interruption: {secret}"),
            ):
                with self.assertRaises(OSError):
                    importer.import_release(
                        receipt.publication_manifest,
                        target,
                        expected_component=manifest.component_ref,
                        import_authorization=import_authorization,
                    )
            imported_interruption = next(
                item
                for item in destination_events.read()
                if item.event_type == "transfer.interrupted"
            )
            self.assertEqual(
                imported_interruption.data,
                {"error_type": "OSError"},
            )
            self.assertNotIn(
                secret,
                destination_events.path.read_text(encoding="utf-8"),
            )

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

    def test_policy_and_exact_authorization_precede_publication_side_effects(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas = FileSystemCAS(root / "cas")
            events = AppendOnlyEventStore(root / "events")
            target = FilesystemPublicationTarget("guarded", root / "published")
            manifest, policy, _, _ = self._manifest(cas, target)
            before = {item.identity for item in cas.iter_refs()}

            with self.assertRaisesRegex(PublicationError, "explicit policy"):
                PublicationService(cas, events).publish(manifest, target)
            self.assertEqual({item.identity for item in cas.iter_refs()}, before)
            self.assertFalse(any(target.blob_root.rglob("*")))

            revoked = replace(
                policy,
                revoked_authorization_ids=(manifest.authorization.authorization_id,),
            )
            with self.assertRaisesRegex(PublicationError, "revoked"):
                PublicationService(
                    cas,
                    events,
                    revoked,
                    clock=lambda: NOW + timedelta(minutes=1),
                ).publish(manifest, target)
            self.assertEqual({item.identity for item in cas.iter_refs()}, before)
            self.assertFalse(any(target.blob_root.rglob("*")))

            with self.assertRaisesRegex(PublicationError, "expired"):
                PublicationService(
                    cas,
                    events,
                    policy,
                    clock=lambda: NOW + timedelta(minutes=6),
                ).publish(manifest, target)
            self.assertEqual({item.identity for item in cas.iter_refs()}, before)
            self.assertFalse(any(target.blob_root.rglob("*")))

            oversized_authorization = PublicationAuthorization.issue(
                manifest.request,
                reason="attempt to bypass the policy lifetime",
                issued_at=NOW,
                expires_at=NOW + timedelta(days=1),
            )
            oversized_manifest = PublicationManifest.create(
                request=manifest.request,
                authorization=oversized_authorization,
                created_at=NOW,
            )
            with self.assertRaisesRegex(PublicationError, "policy lifetime"):
                PublicationService(
                    cas,
                    events,
                    policy,
                    clock=lambda: NOW + timedelta(minutes=1),
                ).publish(oversized_manifest, target)
            self.assertEqual({item.identity for item in cas.iter_refs()}, before)
            self.assertFalse(any(target.blob_root.rglob("*")))

            redirected = FilesystemPublicationTarget(
                target.target_id,
                root / "redirected",
            )
            with self.assertRaisesRegex(PublicationError, "another publisher"):
                PublicationService(
                    cas,
                    events,
                    policy,
                    clock=lambda: NOW + timedelta(minutes=1),
                ).publish(manifest, redirected)
            self.assertFalse(any(redirected.blob_root.rglob("*")))

    def test_request_identity_binds_effective_source_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas = FileSystemCAS(root / "cas")
            target = FilesystemPublicationTarget("release", root / "published")
            manifest, _, _, _ = self._manifest(cas, target)
            authorization = manifest.authorization

            for changed in (
                replace(
                    manifest.request,
                    effective_revision_digest=canonical_identity(
                        {"effective": "other"}
                    ).uri,
                ),
                replace(
                    manifest.request,
                    source_bundle=manifest.request.provenance[0],
                ),
                replace(
                    manifest.request,
                    provenance=(manifest.request.source_bundle,),
                ),
            ):
                with self.subTest(request_digest=changed.digest):
                    with self.assertRaisesRegex(PublicationError, "match"):
                        authorization.require_valid(changed, now=NOW)

    def test_current_import_policy_precedes_local_cas_and_event_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_cas = FileSystemCAS(root / "source")
            target = FilesystemPublicationTarget("trusted-source", root / "published")
            manifest, publication_policy, _, _ = self._manifest(source_cas, target)
            manifest_ref = (
                PublicationService(
                    source_cas,
                    AppendOnlyEventStore(root / "publish-events"),
                    publication_policy,
                    clock=lambda: NOW + timedelta(minutes=1),
                )
                .publish(manifest, target)
                .publication_manifest
            )

            destination = FileSystemCAS(root / "destination")
            events = AppendOnlyEventStore(root / "import-events")
            importer = PublicationService(
                destination,
                events,
                clock=lambda: NOW + timedelta(minutes=1),
            )
            policy, request, authorization = self._import_context(
                importer,
                manifest,
                manifest_ref,
                target,
            )
            before = tuple(destination.iter_refs())

            with self.assertRaisesRegex(PublicationError, "explicit local policy"):
                importer.import_release(
                    manifest_ref,
                    target,
                    expected_component=manifest.component_ref,
                    import_authorization=authorization,
                )
            self.assertEqual(tuple(destination.iter_refs()), before)
            self.assertEqual(events.read(), ())

            for rejected_policy, rejected_authorization, message in (
                (
                    replace(
                        policy,
                        revoked_authorization_ids=(authorization.authorization_id,),
                    ),
                    authorization,
                    "revoked",
                ),
                (
                    replace(
                        policy,
                        revoked_classification_digests=(
                            request.security_classification_digest,
                        ),
                    ),
                    authorization,
                    "classification",
                ),
                (
                    replace(policy, permitted_profiles=(SecurityProfile.REVIEWED,)),
                    authorization,
                    "profile",
                ),
                (
                    policy,
                    ImportAuthorization.issue(
                        request,
                        reason="expired local decision",
                        issued_at=NOW - timedelta(minutes=10),
                        expires_at=NOW - timedelta(minutes=1),
                    ),
                    "expired",
                ),
            ):
                with self.subTest(message=message):
                    importer.import_policy = rejected_policy
                    with self.assertRaisesRegex(PublicationError, message):
                        importer.import_release(
                            manifest_ref,
                            target,
                            expected_component=manifest.component_ref,
                            import_authorization=rejected_authorization,
                        )
                    self.assertEqual(tuple(destination.iter_refs()), before)
                    self.assertEqual(events.read(), ())

    def test_publication_wire_decoders_reject_shape_and_type_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas = FileSystemCAS(root / "cas")
            target = FilesystemPublicationTarget("strict-wire", root / "published")
            manifest, policy, _, _ = self._manifest(cas, target)
            service = PublicationService(
                cas,
                AppendOnlyEventStore(root / "events"),
                policy,
                clock=lambda: NOW + timedelta(minutes=1),
            )
            receipt = service.publish(manifest, target)
            _, import_request, import_authorization = self._import_context(
                PublicationService(
                    FileSystemCAS(root / "destination"),
                    AppendOnlyEventStore(root / "import-events"),
                ),
                manifest,
                receipt.publication_manifest,
                target,
            )

            def clone(document):
                return json.loads(json.dumps(document))

            malformed: list[tuple[str, object, object]] = []

            request_payload = manifest.request.to_dict()
            bad = clone(request_payload)
            bad["actor"] = 7
            malformed.append(("request scalar coercion", PublicationRequest, bad))
            bad = clone(request_payload)
            bad["unexpected"] = "field"
            malformed.append(("request extra field", PublicationRequest, bad))
            bad = clone(request_payload)
            del bad["policy_digest"]
            malformed.append(("request missing field", PublicationRequest, bad))
            bad = clone(request_payload)
            bad["source_bundle"]["size"] = True
            malformed.append(("request boolean blob size", PublicationRequest, bad))
            bad = clone(request_payload)
            bad["provenance"].append(bad["provenance"][0])
            malformed.append(("request duplicate provenance", PublicationRequest, bad))

            publication_auth_payload = manifest.authorization.to_dict()
            bad = clone(publication_auth_payload)
            del bad["revoked"]
            malformed.append(
                ("publication auth missing revoked", PublicationAuthorization, bad)
            )
            bad = clone(publication_auth_payload)
            bad["unexpected"] = None
            malformed.append(
                ("publication auth extra field", PublicationAuthorization, bad)
            )

            manifest_payload = manifest.to_dict()
            bad = clone(manifest_payload)
            bad["schema_version"] = "3"
            malformed.append(("manifest string version", PublicationManifest, bad))
            bad = clone(manifest_payload)
            bad["created_at"] = None
            malformed.append(("manifest null timestamp", PublicationManifest, bad))
            bad = clone(manifest_payload)
            del bad["created_at"]
            malformed.append(("manifest missing field", PublicationManifest, bad))

            import_payload = import_request.to_dict()
            bad = clone(import_payload)
            bad["actor"] = 7
            malformed.append(("import scalar coercion", ImportRequest, bad))
            bad = clone(import_payload)
            del bad["actor"]
            malformed.append(("import missing field", ImportRequest, bad))
            bad = clone(import_payload)
            bad["provenance"].append(bad["provenance"][0])
            malformed.append(("import duplicate provenance", ImportRequest, bad))

            import_auth_payload = import_authorization.to_dict()
            bad = clone(import_auth_payload)
            del bad["revoked"]
            malformed.append(("import auth missing revoked", ImportAuthorization, bad))
            bad = clone(import_auth_payload)
            bad["revoked"] = 0
            malformed.append(("import auth integer revoked", ImportAuthorization, bad))

            receipt_payload = receipt.to_dict()
            bad = clone(receipt_payload)
            bad["operation_id"] = 7
            malformed.append(("receipt scalar coercion", TransferReceipt, bad))
            bad = clone(receipt_payload)
            bad["schema_version"] = "3"
            malformed.append(("receipt string version", TransferReceipt, bad))
            bad = clone(receipt_payload)
            bad["blob_count"] = True
            bad["transferred_count"] = True
            bad["reused_count"] = 0
            malformed.append(("receipt boolean counts", TransferReceipt, bad))
            bad = clone(receipt_payload)
            bad["provenance"].append(bad["provenance"][0])
            malformed.append(("receipt duplicate provenance", TransferReceipt, bad))
            bad = clone(receipt_payload)
            bad["direction"] = "import"
            bad["import_request_digest"] = canonical_identity(
                {"request": "local-import"}
            ).uri
            bad["import_authorization_id"] = 9
            bad["import_policy_digest"] = canonical_identity(
                {"policy": "local-import"}
            ).uri
            malformed.append(("receipt invalid optional type", TransferReceipt, bad))
            bad = clone(receipt_payload)
            bad["direction"] = "import"
            malformed.append(("receipt invalid null optionals", TransferReceipt, bad))
            bad = clone(receipt_payload)
            del bad["import_policy_digest"]
            malformed.append(("receipt missing optional field", TransferReceipt, bad))
            bad = clone(receipt_payload)
            bad["unexpected"] = "field"
            malformed.append(("receipt extra field", TransferReceipt, bad))

            for label, contract, payload in malformed:
                with self.subTest(label=label):
                    with self.assertRaises(ValueError):
                        contract.from_dict(payload)

    def test_publication_objects_cannot_emit_non_schema_scalar_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas = FileSystemCAS(root / "cas")
            target = FilesystemPublicationTarget("strict-object", root / "published")
            manifest, policy, _, _ = self._manifest(cas, target)
            receipt = PublicationService(
                cas,
                AppendOnlyEventStore(root / "events"),
                policy,
                clock=lambda: NOW + timedelta(minutes=1),
            ).publish(manifest, target)

            with self.assertRaisesRegex(ValueError, "size"):
                BlobRef(
                    digest=manifest.request.source_bundle.digest,
                    size=True,
                    media_type=manifest.request.source_bundle.media_type,
                )
            with self.assertRaisesRegex(ValueError, "boolean"):
                replace(manifest.authorization, revoked=1)
            with self.assertRaisesRegex(ValueError, "operation_id"):
                replace(receipt, operation_id=7)
            with self.assertRaisesRegex(ValueError, "integer"):
                replace(
                    receipt,
                    blob_count=True,
                    transferred_count=True,
                    reused_count=0,
                )

    def test_policy_rejects_yolo_provenance_without_explicit_permission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas = FileSystemCAS(root / "cas")
            target = FilesystemPublicationTarget("release", root / "published")
            with self.assertRaisesRegex(PublicationError, "profile"):
                self._manifest(cas, target, security_profile=SecurityProfile.YOLO)


if __name__ == "__main__":
    unittest.main()
