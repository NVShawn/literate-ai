"""Portable BUILD admission preserves current grants and exact PLAN results."""

import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.lifecycle import LocalStandardLifecycleError
from literate_ai.application.standard_build_inputs import (
    StandardBuildInputError,
    validate_standard_build_authority,
)
from literate_ai.application.standard_plan_finalization import (
    StandardPlanFinalizationInputs,
)
from literate_ai.application.standard_project_lifecycle import (
    StandardBuildAuthorization,
    StandardComponentBuildIntent,
    StandardComponentBuildPlan,
    StandardProjectLifecycleError,
)
from literate_ai.contracts import ComponentCommandContract
from literate_ai.security import AuthorizationError
from tests.unit.test_standard_local_command_adapter import (
    _identity,
    _provider_export,
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

    def test_serialized_authority_admits_after_controller_source_is_removed(self):
        plan = StandardComponentBuildPlan.from_dict(self.plan.to_dict())
        inputs = StandardPlanFinalizationInputs(
            StandardComponentBuildIntent.from_dict(self.intent.to_dict()),
            StandardBuildAuthorization.from_dict(self.authorization.to_dict()),
            ComponentCommandContract.from_dict(self.inputs.contract.to_dict()),
            (),
            (),
            "none",
        )
        self.temporary.cleanup()
        validate_standard_build_authority(plan, inputs, now=self.now)

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

    def test_other_valid_authorization_does_not_authorize_existing_plan(self):
        other = replace(
            self.authorization,
            grant=replace(self.authorization.grant, authorization_id="other-grant"),
        )
        inputs = replace(self.inputs, authorization=other)
        other_plan = inputs.finalize()
        self.assertNotEqual(other_plan.identity, self.plan.identity)
        validate_standard_build_authority(other_plan, inputs, now=self.now)
        with self.assertRaisesRegex(StandardBuildInputError, "remote plan differs"):
            validate_standard_build_authority(self.plan, inputs, now=self.now)

    def test_local_admission_does_not_retain_a_plan_with_changed_grant(self):
        bad = replace(
            self.authorization, grant=replace(self.authorization.grant, privileges=())
        )
        with patch.object(self.ports, "_register_finalized_plan") as retain:
            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "index or privileges"
            ):
                self.ports.accept_finalized_plan(self.intent, bad, self.plan)
            retain.assert_not_called()

    def test_changed_command_or_provider_set_cannot_reuse_plan(self):
        contract = self.inputs.contract
        command = replace(
            contract.commands[0], argv=(*contract.commands[0].argv, "changed")
        )
        changed = replace(contract, commands=(command, *contract.commands[1:]))
        with self.assertRaisesRegex(StandardBuildInputError, "remote plan differs"):
            validate_standard_build_authority(
                self.plan, replace(self.inputs, contract=changed), now=self.now
            )
        with self.assertRaisesRegex(StandardProjectLifecycleError, "exact intent"):
            validate_standard_build_authority(
                self.plan,
                replace(self.inputs, providers=(_provider_export("foreign"),)),
                now=self.now,
            )
