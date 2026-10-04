"""Portable BUILD admission preserves current grants and exact PLAN results."""

import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from literate_ai.application.standard_build_inputs import (
    StandardBuildInputError,
    validate_standard_build_authority,
)
from literate_ai.security import AuthorizationError
from tests.support.fixtures_test_standard_local_command_adapter import (
    _identity,
    _python_copy_lifecycle,
)


class StandardBuildAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.ports, _, _, self.intent = _python_copy_lifecycle(
            Path(self.temporary.name)
        )
        self.authorization = self.ports.authorize(self.intent, _identity("index"))
        self.inputs = self.ports.plan_finalization_inputs(
            self.intent, self.authorization
        )
        self.plan = self.inputs.finalize()
        self.now = self.authorization.grant.issued_at + timedelta(seconds=1)

    def test_grant_must_be_current_and_not_revoked(self):
        grant = self.authorization.grant
        for changed, now, code in (
            (grant, grant.issued_at - timedelta(seconds=1), "not_yet_valid"),
            (grant, grant.expires_at, "expired"),
            (replace(grant, revoked=True), self.now, "revoked"),
        ):
            with (
                self.subTest(code=code),
                self.assertRaisesRegex(AuthorizationError, code),
            ):
                validate_standard_build_authority(
                    self.plan,
                    replace(
                        self.inputs,
                        authorization=replace(self.authorization, grant=changed),
                    ),
                    now=now,
                )

    def test_grant_index_and_privileges_cannot_be_substituted(self):
        for grant in (
            replace(
                self.authorization.grant,
                classification_digest=_identity("foreign-index").uri,
            ),
            replace(self.authorization.grant, privileges=()),
        ):
            inputs = replace(
                self.inputs, authorization=replace(self.authorization, grant=grant)
            )
            with self.assertRaisesRegex(StandardBuildInputError, "index or privileges"):
                validate_standard_build_authority(
                    inputs.finalize(), inputs, now=self.now
                )
