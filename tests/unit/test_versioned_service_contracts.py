"""Current writer and narrow legacy-reader coverage for post-tag contract families."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.contracts import (
    ComponentCoordinate,
    ComponentRevisionRef,
    MigrationError,
    canonical_identity,
)
from literate_ai.models import (
    DataEgress,
    Locality,
    ModelEndpoint,
    ModelGroup,
    ModelRouter,
    StageModelPolicy,
    normalize_model_routing_document,
)
from literate_ai.publication import (
    FilesystemPublicationTarget,
    ImportPolicy,
    ImportRequest,
    PublicationManifest,
    PublicationPolicy,
    PublicationRequest,
    PublicationService,
    TransferReceipt,
    normalize_publication_document,
)
from literate_ai.security import (
    AuthorizationRevocationSet,
    BuildRequest,
    FindingSeverity,
    ObservationExecutionAuthorization,
    ObservationRequest,
    OriginAttestation,
    RuleBasedSourceScanner,
    SecurityClassification,
    SecurityFinding,
    SecurityPolicy,
    SourceModule,
    baseline_python_rules,
    normalize_security_document,
)
from literate_ai.storage import AppendOnlyEventStore, FileSystemCAS
from tests.unit.test_schema_catalog import SchemaCatalog

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
NOW = datetime(2026, 8, 5, tzinfo=UTC)
COMPATIBILITY_GOLDEN = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "schemas"
    / "v0.1.1"
    / "compatibility-cases.json"
)
COMPATIBILITY_MATRIX = (
    Path(__file__).resolve().parents[2] / "schemas" / "v2" / "compatibility.json"
)


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class ModelRoutingWireVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaCatalog()
        self.endpoint = ModelEndpoint(
            endpoint_id="local",
            provider="test",
            model="generator",
            base_url="http://127.0.0.1:8080",
            locality=Locality.LOCAL,
            capabilities=("structured",),
            context_tokens=4096,
        )
        self.group = ModelGroup(
            "generation", "1.0.0", (self.endpoint.endpoint_id,), (self.endpoint.ref,)
        )
        self.policy = StageModelPolicy(
            "generate",
            "generate",
            self.group.group_id,
            required_capabilities=("structured",),
            data_egress=DataEgress.NONE,
            group_ref=self.group.ref,
        )
        self.decision = ModelRouter(
            endpoints=(self.endpoint,), groups=(self.group,)
        ).select(self.policy)

    def test_current_writers_emit_v2_and_exact_readers_round_trip(self) -> None:
        for record in (self.endpoint, self.group, self.policy, self.decision):
            with self.subTest(record=type(record).__name__):
                wire = record.to_dict()
                self.assertEqual(wire["schema"], record.SCHEMA)
                self.schemas.validate(record.SCHEMA, wire)
                self.assertEqual(type(record).from_dict(wire), record)

                legacy = dict(wire)
                legacy.pop("schema")
                self.assertEqual(type(record).from_dict(legacy), record)

    def test_unknown_or_mismatched_routing_versions_fail_closed(self) -> None:
        unknown = dict(self.endpoint.to_dict())
        unknown["schema"] = "urn:literate-ai:schema:v3:model-endpoint"
        with self.assertRaisesRegex(ValueError, "unsupported"):
            normalize_model_routing_document(unknown)
        with self.assertRaisesRegex(ValueError, "expected"):
            ModelGroup.from_dict(self.endpoint.to_dict())


class SecurityWireVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaCatalog()
        self.policy = SecurityPolicy(policy_digest=DIGEST_C)
        self.attestation = OriginAttestation(
            source_digest=DIGEST_A,
            signer="release@example.test",
            trust_root="test-root",
            signature_identity="sigstore:test",
            verified=True,
        )
        self.finding = SecurityFinding(
            finding_id="finding:test",
            source_digest=DIGEST_A,
            category="test",
            severity=FindingSeverity.LOW,
            scanner_identity="scanner:test@1",
            message="fixture",
        )
        self.classification = self.policy.classify(
            effective_revision_digest=DIGEST_B,
            attestations=(self.attestation,),
            findings=(self.finding,),
        )
        self.build = BuildRequest(
            effective_revision_digest=DIGEST_B,
            source_bundle_digest=DIGEST_A,
            builder_id="builder:test@1",
            toolchain_digest=DIGEST_C,
            sandbox_profile="constrained",
            requested_privileges=("compiler",),
            allowed_outputs=("artifact",),
        )
        self.build_authorization = self.policy.authorize_build(
            self.classification,
            self.build,
            actor="reviewer",
            reason="verified fixture",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        self.observation = ObservationRequest(
            effective_revision_digest=DIGEST_B,
            source_digests=(DIGEST_A,),
            runner_id="runner:test@1",
            harness_digest=DIGEST_C,
            sandbox_profile="constrained",
            requested_privileges=(),
            allowed_outputs=("report",),
        )
        self.observation_authorization = self.policy.authorize_observation(
            self.classification,
            self.observation,
            actor="reviewer",
            reason="verified fixture",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        self.report = RuleBasedSourceScanner(
            "scanner:baseline-python@1", baseline_python_rules()
        ).scan((SourceModule.create("src/app.py", b"exec(user_input)\n"),))
        self.revocations = AuthorizationRevocationSet().revoke(
            self.build_authorization.authorization_id,
            actor="security",
            reason="test revocation",
        )

    def test_current_writers_emit_v2_and_exact_readers_round_trip(self) -> None:
        records = (
            self.attestation,
            self.finding,
            self.classification,
            self.build,
            self.build_authorization,
            self.observation,
            self.observation_authorization,
            self.report,
            self.revocations,
        )
        for record in records:
            with self.subTest(record=type(record).__name__):
                wire = record.to_dict()
                self.assertEqual(wire["schema"], record.SCHEMA)
                self.schemas.validate(record.SCHEMA, wire)
                self.assertEqual(type(record).from_dict(wire), record)

                legacy = deepcopy(wire)
                legacy.pop("schema")
                if record is self.report:
                    for finding in legacy["findings"]:
                        finding.pop("schema")
                self.assertEqual(type(record).from_dict(legacy), record)

    def test_unknown_security_version_fails_closed(self) -> None:
        unknown = dict(self.build.to_dict())
        unknown["schema"] = "urn:literate-ai:schema:v99:build-request"
        with self.assertRaisesRegex(ValueError, "unsupported"):
            normalize_security_document(unknown)

    def test_published_v1_security_golden_subset_is_explicit(self) -> None:
        golden = json.loads(COMPATIBILITY_GOLDEN.read_bytes())["security"]
        matrix = json.loads(COMPATIBILITY_MATRIX.read_bytes())
        read_paths = {item["input"]: item for item in matrix["read_paths"]}
        self.assertEqual(
            read_paths["urn:literate-ai:schema:v1:security-classification"][
                "condition"
            ],
            "source-digests-and-origin-attestation-digests-nonempty",
        )
        self.assertEqual(
            read_paths["urn:literate-ai:schema:v1:observation-request"]["condition"],
            "source-digests-and-allowed-outputs-nonempty",
        )
        incompatible = {item["input"]: item for item in matrix["incompatible_inputs"]}
        self.assertEqual(
            incompatible[
                "urn:literate-ai:schema:v1:observation-execution-authorization"
            ]["migration"],
            "fail-closed-missing-effective-revision-and-security-profile",
        )
        supported = (
            (
                "urn:literate-ai:schema:v1:security-classification",
                SecurityClassification,
                "historical_classification_supported",
                self.classification,
            ),
            (
                "urn:literate-ai:schema:v1:observation-request",
                ObservationRequest,
                "historical_observation_request_supported",
                self.observation,
            ),
        )
        for schema, contract, name, expected in supported:
            with self.subTest(case=name):
                document = golden[name]
                self.schemas.validate(schema, document)
                self.assertEqual(contract.from_dict(document), expected)

        classification = golden["historical_classification_supported"]
        observation = golden["historical_observation_request_supported"]
        constrained_incompatible = (
            (
                "urn:literate-ai:schema:v1:security-classification",
                SecurityClassification,
                dict(classification, source_digests=[]),
                "source_digests cannot be empty",
            ),
            (
                "urn:literate-ai:schema:v1:security-classification",
                SecurityClassification,
                dict(classification, origin_attestation_digests=[]),
                "origin_attestation_digests cannot be empty",
            ),
            (
                "urn:literate-ai:schema:v1:observation-request",
                ObservationRequest,
                dict(observation, source_digests=[]),
                "source_digests cannot be empty",
            ),
            (
                "urn:literate-ai:schema:v1:observation-request",
                ObservationRequest,
                dict(observation, allowed_outputs=[]),
                "allowed_outputs cannot be empty",
            ),
        )
        for schema, contract, document, message in constrained_incompatible:
            with self.subTest(schema=schema, message=message):
                self.schemas.validate(schema, document)
                with self.assertRaisesRegex(ValueError, message):
                    contract.from_dict(document)

        self.assertEqual(
            golden["historical_classification_empty_evidence"],
            dict(classification, source_digests=[], origin_attestation_digests=[]),
        )
        self.assertEqual(
            golden["historical_observation_request_empty_scope"],
            dict(observation, source_digests=[], allowed_outputs=[]),
        )
        authorization = golden["historical_observation_execution_authorization"]
        self.schemas.validate(
            "urn:literate-ai:schema:v1:observation-execution-authorization",
            authorization,
        )
        with self.assertRaisesRegex(ValueError, "fields do not match"):
            ObservationExecutionAuthorization.from_dict(authorization)


class PublicationWireVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaCatalog()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cas = FileSystemCAS(self.root / "cas")
        self.target = FilesystemPublicationTarget("team-cache", self.root / "published")
        source = self.cas.put_bytes(b"print('hello')\n", media_type="text/x-python")
        provenance = self.cas.put_bytes(
            b'{"generator":"deterministic"}\n', media_type="application/json"
        )
        component_ref = ComponentRevisionRef(
            ComponentCoordinate("test", "demo-component"),
            "1.0.0",
            canonical_identity({"component": "demo-component", "version": "1.0.0"}),
        )
        self.policy = PublicationPolicy(
            DIGEST_C, allowed_targets=(self.target.target_id,)
        )
        request = PublicationRequest.create(
            component_ref=component_ref,
            component_lock_identity=canonical_identity(
                {"component-lock": "demo-component"}
            ),
            effective_revision_digest=DIGEST_B,
            source_bundle=source,
            roots={"source": source, "provenance": provenance},
            blobs=(source, provenance),
            provenance=(provenance,),
            security_classification_digest=DIGEST_A,
            security_profile=self.policy.permitted_profiles[0],
            target_id=self.target.target_id,
            target_identity_digest=self.target.identity,
            policy_digest=self.policy.policy_digest,
            actor="release-manager",
        )
        authorization = self.policy.authorize(
            request,
            reason="publish fixture",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        self.manifest = PublicationManifest.create(
            request=request, authorization=authorization, created_at=NOW
        )
        service = PublicationService(
            self.cas,
            AppendOnlyEventStore(self.root / "events"),
            self.policy,
            clock=lambda: NOW + timedelta(minutes=1),
        )
        self.receipt = service.publish(self.manifest, self.target)
        import_policy = ImportPolicy(
            policy_digest=DIGEST_B,
            allowed_source_targets=(self.target.target_id,),
            allowed_classification_digests=(DIGEST_A,),
            trusted_publication_policy_digests=(DIGEST_C,),
        )
        self.import_request = ImportRequest.create(
            manifest=self.manifest,
            publication_manifest=self.receipt.publication_manifest,
            destination_identity_digest=service.import_destination_identity,
            policy_digest=import_policy.policy_digest,
            actor="importer",
        )
        self.import_authorization = import_policy.authorize(
            self.import_request,
            reason="import fixture",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_current_publication_writers_emit_v2_v5(self) -> None:
        records = (
            self.manifest.request,
            self.manifest.authorization,
            self.manifest,
            self.import_request,
            self.import_authorization,
            self.receipt,
        )
        for record in records:
            with self.subTest(record=type(record).__name__):
                wire = record.to_dict()
                self.assertEqual(wire["schema"], record.SCHEMA)
                if record is self.manifest or record is self.receipt:
                    self.assertEqual(wire["schema_version"], 5)
                self.schemas.validate(record.SCHEMA, wire)
                self.assertEqual(type(record).from_dict(wire), record)

    def test_post_tag_v3_manifest_and_receipt_migrate_narrowly(self) -> None:
        legacy_manifest = deepcopy(self.manifest.to_dict())
        legacy_manifest.pop("schema")
        legacy_manifest["schema_version"] = 3
        legacy_request = legacy_manifest["request"]
        legacy_request.pop("schema")
        legacy_request.pop("component_lock_identity")
        legacy_authorization = legacy_manifest["authorization"]
        legacy_authorization.pop("schema")
        legacy_authorization["request_digest"] = _digest(legacy_request)
        authorization_material = dict(legacy_authorization)
        authorization_material.pop("authorization_id")
        authorization_material.pop("revoked")
        legacy_authorization["authorization_id"] = (
            "publication-auth:"
            + _digest(authorization_material).removeprefix("sha256:")[:24]
        )

        migrated_manifest = PublicationManifest.from_dict(
            legacy_manifest,
            legacy_component_lock_identity=self.manifest.component_lock_identity,
        )
        self.assertEqual(migrated_manifest.schema_version, 5)
        self.assertEqual(
            migrated_manifest.authorization.request_digest,
            migrated_manifest.request.digest,
        )
        self.schemas.validate(PublicationManifest.SCHEMA, migrated_manifest.to_dict())
        self.assertEqual(
            normalize_publication_document(
                legacy_manifest,
                legacy_component_lock_identity=self.manifest.component_lock_identity,
            ),
            migrated_manifest.to_dict(),
        )

        legacy_receipt = deepcopy(self.receipt.to_dict())
        legacy_receipt.pop("schema")
        legacy_receipt["schema_version"] = 3
        legacy_receipt.pop("component_lock_identity")
        migrated_receipt = TransferReceipt.from_dict(
            legacy_receipt,
            legacy_manifest=self.manifest,
            legacy_manifest_document=self.manifest.to_dict(),
        )
        self.assertEqual(migrated_receipt.schema_version, 5)
        self.schemas.validate(TransferReceipt.SCHEMA, migrated_receipt.to_dict())
        self.assertEqual(
            normalize_publication_document(
                legacy_receipt,
                legacy_manifest=self.manifest,
                legacy_manifest_document=self.manifest.to_dict(),
            ),
            migrated_receipt.to_dict(),
        )

    def test_v4_manifest_requires_exact_lock_context_and_reissues_authority(
        self,
    ) -> None:
        legacy = deepcopy(self.manifest.to_dict())
        legacy["schema_version"] = 4
        legacy_request = legacy["request"]
        legacy_request.pop("component_lock_identity")
        authorization = legacy["authorization"]
        authorization["request_digest"] = _digest(legacy_request)
        authorization_material = dict(authorization)
        authorization_material.pop("schema")
        authorization_material.pop("authorization_id")
        authorization_material.pop("revoked")
        authorization["authorization_id"] = (
            "publication-auth:"
            + _digest(authorization_material).removeprefix("sha256:")[:24]
        )

        with self.assertRaisesRegex(MigrationError, "Component lock"):
            PublicationManifest.from_dict(legacy)
        migrated = PublicationManifest.from_dict(
            legacy,
            legacy_component_lock_identity=self.manifest.component_lock_identity,
        )
        self.assertEqual(migrated.schema_version, 5)
        self.assertEqual(
            migrated.authorization.request_digest,
            migrated.request.digest,
        )
        self.assertEqual(
            normalize_publication_document(
                legacy,
                legacy_component_lock_identity=(self.manifest.component_lock_identity),
            ),
            migrated.to_dict(),
        )

    def test_lock_omission_and_substitution_fail_closed(self) -> None:
        legacy_request = deepcopy(self.manifest.request.to_dict())
        legacy_request.pop("component_lock_identity")
        with self.assertRaisesRegex(MigrationError, "Component lock"):
            PublicationRequest.from_dict(legacy_request)
        self.assertEqual(
            PublicationRequest.from_dict(
                legacy_request,
                legacy_component_lock_identity=(self.manifest.component_lock_identity),
            ),
            self.manifest.request,
        )

        legacy_import = deepcopy(self.import_request.to_dict())
        legacy_import.pop("component_lock_identity")
        with self.assertRaisesRegex(MigrationError, "Component lock"):
            ImportRequest.from_dict(legacy_import)
        self.assertEqual(
            ImportRequest.from_dict(
                legacy_import,
                legacy_component_lock_identity=(self.manifest.component_lock_identity),
            ),
            self.import_request,
        )

        substituted = deepcopy(self.manifest.to_dict())
        substituted["request"]["component_lock_identity"] = canonical_identity(
            {"component-lock": "substituted"}
        ).to_dict()
        with self.assertRaisesRegex(RuntimeError, "does not match request"):
            PublicationManifest.from_dict(substituted)

    def test_published_v1_publication_goldens_require_exact_context(self) -> None:
        golden = json.loads(COMPATIBILITY_GOLDEN.read_bytes())["publication"]
        matrix = json.loads(COMPATIBILITY_MATRIX.read_bytes())
        published = {
            item["input"]: item
            for item in matrix["read_paths"]
            if item["status"] == "published-context-required"
        }
        self.assertEqual(
            published["urn:literate-ai:schema:v1:publication-manifest"][
                "required_context"
            ],
            [
                "verified-v2-publication-request",
                "verified-v2-publication-authorization",
            ],
        )
        self.assertEqual(
            published["urn:literate-ai:schema:v1:transfer-receipt"]["required_context"],
            ["verified-v2-publication-manifest-bytes"],
        )
        historical_manifest = golden["historical_manifest"]
        historical_receipt = golden["historical_receipt"]
        migration_lock = canonical_identity({"component-lock": "v0.1.1-context"})
        verified_manifest = PublicationManifest.from_dict(
            golden["verified_v2_manifest_context"],
            legacy_component_lock_identity=migration_lock,
        )
        self.schemas.validate(
            "urn:literate-ai:schema:v1:publication-manifest",
            historical_manifest,
        )
        self.schemas.validate(
            "urn:literate-ai:schema:v1:transfer-receipt",
            historical_receipt,
        )

        with self.assertRaisesRegex(MigrationError, "lacks source"):
            PublicationManifest.from_dict(historical_manifest)
        migrated_manifest = PublicationManifest.from_dict(
            historical_manifest,
            legacy_request=verified_manifest.request,
            legacy_authorization=verified_manifest.authorization,
        )
        self.assertEqual(migrated_manifest, verified_manifest)

        with self.assertRaisesRegex(MigrationError, "lacks source"):
            TransferReceipt.from_dict(historical_receipt)
        migrated_receipt = TransferReceipt.from_dict(
            historical_receipt,
            legacy_manifest=migrated_manifest,
            legacy_manifest_document=golden["verified_v2_manifest_context"],
        )
        self.assertEqual(
            migrated_receipt.publication_manifest.to_dict(),
            historical_receipt["publication_manifest"],
        )
        self.assertEqual(
            migrated_receipt.effective_revision_digest,
            migrated_manifest.request.effective_revision_digest,
        )
        self.assertEqual(
            migrated_receipt.publication_request_digest,
            migrated_manifest.request.digest,
        )

        alternate_source = self.cas.put_bytes(
            b"print('substituted')\n", media_type="text/x-python"
        )
        alternate_provenance = self.cas.put_bytes(
            b'{"generator":"substituted"}\n', media_type="application/json"
        )
        alternate_request = PublicationRequest.create(
            component_ref=verified_manifest.component_ref,
            component_lock_identity=verified_manifest.component_lock_identity,
            effective_revision_digest=verified_manifest.request.effective_revision_digest,
            source_bundle=alternate_source,
            roots={
                "source": alternate_source,
                "provenance": alternate_provenance,
            },
            blobs=(alternate_source, alternate_provenance),
            provenance=(alternate_provenance,),
            security_classification_digest=(
                verified_manifest.request.security_classification_digest
            ),
            security_profile=verified_manifest.request.security_profile,
            target_id=verified_manifest.request.target_id,
            target_identity_digest=verified_manifest.request.target_identity_digest,
            policy_digest=verified_manifest.request.policy_digest,
            actor=verified_manifest.request.actor,
        )
        alternate_authorization = self.policy.authorize(
            alternate_request,
            reason="substituted context",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        alternate_manifest = PublicationManifest.create(
            request=alternate_request,
            authorization=alternate_authorization,
            created_at=NOW,
        )
        with self.assertRaisesRegex(MigrationError, "exact supplied"):
            TransferReceipt.from_dict(
                historical_receipt,
                legacy_manifest=alternate_manifest,
                legacy_manifest_document=alternate_manifest.to_dict(),
            )

        mismatched_request = deepcopy(verified_manifest.request.to_dict())
        mismatched_request["roots"]["unexpected-alias"] = mismatched_request["roots"][
            "source"
        ]
        with self.assertRaisesRegex(MigrationError, "do not match supplied"):
            PublicationManifest.from_dict(
                historical_manifest,
                legacy_request=PublicationRequest.from_dict(mismatched_request),
                legacy_authorization=verified_manifest.authorization,
            )

    def test_unknown_publication_versions_fail_closed(self) -> None:
        unknown = dict(self.manifest.request.to_dict())
        unknown["schema"] = "urn:literate-ai:schema:v3:publication-request"
        with self.assertRaisesRegex(MigrationError, "unsupported"):
            normalize_publication_document(unknown)

        future = dict(self.receipt.to_dict())
        future["schema_version"] = 6
        with self.assertRaisesRegex(MigrationError, "future"):
            TransferReceipt.from_dict(future)


if __name__ == "__main__":
    unittest.main()
