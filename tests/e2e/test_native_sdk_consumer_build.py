"""Actual Standard consumer builds with a real compiled SDK and fixture source."""

import unittest
from unittest.mock import patch

from literate_ai.adapters.qualification_capture import (
    QualificationEvidenceRecorder,
)
from literate_ai.security import AuthorizationError
from tests.support import (
    fixtures_test_native_sdk_standard_authority as test_native_sdk_standard_authority,
)

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
