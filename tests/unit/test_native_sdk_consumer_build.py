"""Actual Standard consumer builds with a real compiled SDK and fixture source."""

import copy
import json
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from literate_ai.adapters.lifecycle import LocalStandardLifecycleError
from literate_ai.adapters.native_sdk_custody import materialize_native_sdk
from literate_ai.adapters.native_sdk_qualification import sdk_process_fields
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
    verify_qualification_build,
    verify_qualification_build_authorization,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.security import AuthorizationError
from literate_ai.storage.cas import BlobIntegrityError
from tests.support import fixtures_test_native_sdk_standard_authority as test_native_sdk_standard_authority

_CAPTURE_MAX_BYTES = 80_000_000


class NativeSdkConsumerBuildTests(unittest.TestCase):
    def setUp(self):
        self.fixture = (
            test_native_sdk_standard_authority.NativeSdkStandardAuthorityTests()
        )
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.ports = self.fixture.ports
        self.recorder = QualificationEvidenceRecorder(
            max_bytes=_CAPTURE_MAX_BYTES, max_records=2000
        )
        self.ports.retain_evidence_with(self.recorder)
        self.plan, _, _ = self.fixture.prepare_standard_build(
            extra_source_files={
                "sdk_metadata.py": "from literate_ai_native_sdk import binding\n"
            }
        )

    def test_build_retains_sdk_bom_and_rechecks_custody_on_cache_hit(self):
        with patch.object(self.ports, "_run_sdk_locked") as runtime:
            built = self.ports.build(self.plan, ())
            runtime.assert_not_called()
        verify_qualification_build(
            QualificationEvidenceReader(
                self.recorder.entries,
                max_bytes=_CAPTURE_MAX_BYTES,
                max_records=2000,
            ),
            plan=self.plan,
            build=built.evidence,
        )
        reader = QualificationEvidenceReader(
            self.recorder.entries,
            max_bytes=_CAPTURE_MAX_BYTES,
            max_records=2000,
        )
        grant = self.ports._sdk_build_authorizations[self.plan.identity.uri][1]
        verify_qualification_build_authorization(
            reader,
            candidate=self.fixture.candidate,
            index_identity=ContentIdentity.parse_uri(grant.classification_digest),
            authorization_identity=self.plan.request.authorization_identity,
            plan=self.plan,
        )
        observation = reader.read_json(built.evidence.build_observation_identity)
        process = reader.read_json(
            ContentIdentity.parse_uri(observation["process_observation_identity"])
        )
        original = reader.read_json(
            ContentIdentity.parse_uri(process["native_sdk_build_identity"])
        )
        for mutate in (
            lambda value: value.update(
                schema="literate-ai/native-sdk-command-execution@2"
            ),
            lambda value: value.update(dependencies=[]),
            lambda value: value.update(checks=[]),
            lambda value: value["checks"][1].update(revocations=[]),
            lambda value: value["command_binding"].update(phase="execute"),
            lambda value: value["authorization"].update(privileges=[]),
        ):
            changed = copy.deepcopy(original)
            mutate(changed)
            mutation_recorder = QualificationEvidenceRecorder(
                max_bytes=2 * _CAPTURE_MAX_BYTES, max_records=2001
            )
            for identity, payload in self.recorder.entries:
                mutation_recorder._remember(identity, payload)
            identity = mutation_recorder.remember_json(changed)
            with self.assertRaises(QualificationCaptureError):
                sdk_process_fields(
                    QualificationEvidenceReader(
                        mutation_recorder.entries,
                        max_bytes=2 * _CAPTURE_MAX_BYTES,
                        max_records=2001,
                    ),
                    plan=self.plan,
                    process={**process, "native_sdk_build_identity": identity.uri},
                    phase="build",
                )
        artifact = self.ports._artifact_paths[built.exports[0].identity.uri]
        self.assertEqual(
            (artifact / self.fixture.contract.artifact_export.export_id).read_bytes(),
            b"known-output\n",
        )
        components = [
            item
            for path in artifact.rglob("*.json")
            for item in json.loads(path.read_bytes()).get("components", [])
            if item["bom-ref"].startswith("urn:literate-ai:native-sdk:")
        ]
        self.assertEqual(len(components), 1)
        ids = self.plan.materialization.native_sdk_input_identities
        self.assertIn(
            {"name": "literate-ai:native-sdk-input", "value": ids[0].uri},
            components[0]["properties"],
        )
        old_key = canonical_identity(
            {
                "schema": "literate-ai/local-standard-build-cache-key@2",
                "command_contract_identity": self.fixture.contract.identity.uri,
                "source_tree_identity": self.plan.request.source_tree_identity.uri,
                "provider_materials": (),
            }
        )
        self.assertNotEqual(artifact.name, old_key.digest)
        with patch.object(self.ports, "_run_locked") as run:
            cached = self.ports.build(self.plan, ())
            run.assert_not_called()
        self.assertEqual(cached.exports, built.exports)
        self.assertEqual(self.ports.build_cache_hits, 1)
        native = next(
            item
            for item in self.fixture.built.product.snapshot.files
            if item.path == self.fixture.built.product.snapshot.native_libraries[0]
        )
        blob = self.fixture.service.store.path_for(native.blob)
        blob.chmod(0o600)
        blob.write_bytes(blob.read_bytes() + b"changed")
        with patch.object(self.ports, "_build_locked") as dispatch:
            with self.assertRaises(BlobIntegrityError):
                self.ports.build(self.plan, ())
            dispatch.assert_not_called()

    def test_missing_or_revoked_build_grant_refuses_before_dispatch(self):
        key = self.plan.identity.uri
        authority = self.ports._sdk_build_authorizations.pop(key)
        with patch.object(self.ports, "_build_locked") as dispatch:
            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "no finalized authorization"
            ):
                self.ports.build(self.plan, ())
            dispatch.assert_not_called()
        self.ports._sdk_build_authorizations[key] = (
            authority[0],
            replace(authority[1], privileges=()),
        )
        with patch.object(self.ports, "_build_locked") as dispatch:
            with self.assertRaises(AuthorizationError):
                self.ports.build(self.plan, ())
            dispatch.assert_not_called()
        self.ports._sdk_build_authorizations[key] = authority
        clock = self.ports.clock
        self.ports.clock = lambda: authority[1].expires_at + timedelta(seconds=1)
        with patch.object(self.ports, "_build_locked") as dispatch:
            with self.assertRaises(AuthorizationError):
                self.ports.build(self.plan, ())
            dispatch.assert_not_called()
        self.ports.clock = clock
        self.fixture.fixture.revocations = self.fixture.fixture.revocations.revoke(
            authority[1].authorization_id, actor="operator", reason="stop build"
        )
        with patch.object(self.ports, "_build_locked") as dispatch:
            with self.assertRaises(AuthorizationError):
                self.ports.build(self.plan, ())
            dispatch.assert_not_called()
        self.assertEqual(list(self.ports.object_root.iterdir()), [])

    def test_revocation_after_compilation_prevents_artifact_publication(self):
        run = self.ports._run_with_environment
        grant = self.ports._sdk_build_authorizations[self.plan.identity.uri][1]

        def revoke_after_run(*args, **kwargs):
            result = run(*args, **kwargs)
            self.fixture.fixture.revocations = self.fixture.fixture.revocations.revoke(
                grant.authorization_id, actor="operator", reason="stop build"
            )
            return result

        with patch.object(
            self.ports, "_run_with_environment", side_effect=revoke_after_run
        ):
            with self.assertRaises(AuthorizationError):
                self.ports.build(self.plan, ())
        self.assertEqual(list(self.ports.object_root.iterdir()), [])
        self.assertEqual(self.ports.build_cache_misses, 0)

    def test_materialized_sdk_drift_prevents_artifact_publication(self):
        roots = []
        run = self.ports._run_with_environment

        def materialize(*args, **kwargs):
            root = materialize_native_sdk(*args, **kwargs)
            roots.append(root)
            return root

        def mutate_after_run(*args, **kwargs):
            result = run(*args, **kwargs)
            image = roots[0] / self.fixture.built.product.snapshot.native_libraries[0]
            image.write_bytes(image.read_bytes() + b"drift")
            return result

        with (
            patch(
                "literate_ai.adapters.native_sdk_consumer.materialize_native_sdk",
                side_effect=materialize,
            ),
            patch.object(
                self.ports, "_run_with_environment", side_effect=mutate_after_run
            ),
        ):
            with self.assertRaisesRegex(ValueError, "SDK consumer input changed"):
                self.ports.build(self.plan, ())
        self.assertEqual(list(self.ports.object_root.iterdir()), [])
        self.assertEqual(self.ports.build_cache_misses, 0)
