"""A scoped build grant cannot be reused for a different containment closure."""

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

from literate_ai.adapters.build_grant_consumption import (
    SQLiteBuildGrantConsumptionStore,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import canonical_identity
from literate_ai.security.build_admission import SingleUseBuildAdmission
from literate_ai.security.isolation.build_binding import ProductionBuildBinding
from literate_ai.security.isolation.contracts import (
    ContainmentStage,
    IsolationLevel,
    IsolationPolicy,
    IsolationRequest,
    StageIsolationRule,
)
from literate_ai.security.policy import (
    AuthorizationError,
    BuildAuthorization,
    BuildRequest,
    SecurityProfile,
)

DIGEST = "sha256:" + "a" * 64
NOW = datetime(2026, 9, 13, tzinfo=UTC)


class ProductionBuildBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = SQLiteBuildGrantConsumptionStore.initialize(
            Path(temporary.name).resolve() / "admission.db"
        )
        request = BuildRequest(
            DIGEST, DIGEST, "builder", DIGEST, "unbound", (), ("out",)
        )
        self.binding = ProductionBuildBinding(
            IsolationRequest(
                "build-one",
                ContainmentStage.BUILD,
                DIGEST,
                "linux",
                "x86_64",
                IsolationLevel.OS_SANDBOXED,
            ),
            IsolationPolicy(
                "production",
                (
                    StageIsolationRule(
                        ContainmentStage.BUILD,
                        IsolationLevel.OS_SANDBOXED,
                        (),
                    ),
                ),
            ),
            "trusted-runner-one",
            *(BlobRef(character * 64, 12) for character in "1234"),
        )
        self.request = self.binding.bind(request)
        self.grant = BuildAuthorization(
            "one",
            DIGEST,
            canonical_identity(self.request.to_dict()).uri,
            DIGEST,
            "actor",
            "test",
            SecurityProfile.CONSTRAINED,
            (),
            NOW,
            NOW + timedelta(hours=1),
        )

    def admission(self, verifier=None):
        return SingleUseBuildAdmission(
            verifier if verifier is not None else Mock(), self.store, lambda: NOW
        )

    def test_bound_grant_succeeds_once_and_preserves_other_request_fields(self):
        self.assertEqual(self.binding.bind(self.request), self.request)
        admission = self.admission()
        admission.admit_contained_build(self.grant, self.request, self.binding)
        with self.assertRaisesRegex(AuthorizationError, "already_consumed"):
            admission.admit_contained_build(self.grant, self.request, self.binding)

    def test_substituted_closure_refuses_before_consumption(self):
        changes = [
            {"runner_id": "other-runner"},
            {"policy": replace(self.binding.policy, policy_id="other-policy")},
            {"isolation": replace(self.binding.isolation, target_os="windows")},
            {"isolation": replace(self.binding.isolation, target_architecture="arm64")},
            {"isolation": replace(self.binding.isolation, operation_id="build-two")},
        ]
        for field in ("runtime", "image", "configuration", "host_profile"):
            reference = getattr(self.binding, field)
            for change in (
                {"digest": "f" * 64},
                {"size": reference.size + 1},
                {"media_type": "application/json"},
            ):
                changes.append({field: replace(reference, **change)})
        verifier = Mock()
        admission = self.admission(verifier)
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaisesRegex(AuthorizationError, "binding_mismatch"):
                    admission.admit_contained_build(
                        self.grant, self.request, replace(self.binding, **change)
                    )
        verifier.require_build_valid.assert_not_called()
        admission.admit_contained_build(self.grant, self.request, self.binding)

    def test_rebinding_cannot_reuse_the_old_grant(self):
        other = replace(self.binding, runner_id="other-runner")
        with self.assertRaisesRegex(AuthorizationError, "request_mismatch"):
            self.admission().admit_contained_build(
                self.grant, other.bind(self.request), other
            )
        self.admission().admit_contained_build(self.grant, self.request, self.binding)

    def test_wrong_subject_stage_and_local_fallback_refuse(self):
        with self.assertRaisesRegex(AuthorizationError, "subject_mismatch"):
            self.binding.bind(
                replace(self.request, source_bundle_digest="sha256:" + "f" * 64)
            )
        for change, reason in (
            ({"stage": ContainmentStage.GENERATED_TEST}, "stage_mismatch"),
            ({"host_yolo_acknowledged": True}, "production_containment_required"),
        ):
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(AuthorizationError, reason),
            ):
                replace(
                    self.binding, isolation=replace(self.binding.isolation, **change)
                )
        with self.assertRaisesRegex(
            AuthorizationError, "production_containment_required"
        ):
            replace(
                self.binding,
                isolation=replace(
                    self.binding.isolation,
                    requested_level=IsolationLevel.PROCESS_LIMITED,
                ),
                policy=replace(
                    self.binding.policy,
                    rules=(
                        StageIsolationRule(
                            ContainmentStage.BUILD, IsolationLevel.PROCESS_LIMITED, ()
                        ),
                    ),
                ),
            )
