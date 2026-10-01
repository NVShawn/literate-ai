"""Portable authorization preserves admitted intent and controller policy."""

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from literate_ai.application.standard_authorization import StandardAuthorizationInputs
from literate_ai.application.standard_project_lifecycle import (
    StandardComponentBuildIntent,
)
from literate_ai.security import AuthorizationError, SecurityProfile
from tests.unit.test_standard_local_command_adapter import (
    _identity,
    _python_copy_lifecycle,
)


class StandardAuthorizationTests(unittest.TestCase):
    def test_serialized_intent_and_explicit_time_preserve_bounded_grant(self):
        with tempfile.TemporaryDirectory() as scratch:
            _, _, _, intent = _python_copy_lifecycle(Path(scratch))
            wire = json.loads(json.dumps(intent.to_dict()))
        intent = StandardComponentBuildIntent.from_dict(wire)
        index = _identity("completed-index")
        issued = datetime(2026, 9, 30, tzinfo=UTC)
        authorization = StandardAuthorizationInputs(intent, index, issued).authorize()
        self.assertEqual(authorization.build_intent_identity, intent.identity)
        self.assertEqual(authorization.index_identity, index)
        self.assertEqual(authorization.grant.profile, SecurityProfile.CONSTRAINED)
        self.assertEqual(
            authorization.grant.privileges, intent.build_request.requested_privileges
        )
        self.assertEqual(authorization.grant.issued_at, issued)
        self.assertEqual(authorization.grant.expires_at, issued + timedelta(minutes=30))
        authorization.grant.require_valid(intent.build_request, now=issued)
        with self.assertRaises(AuthorizationError):
            authorization.grant.require_valid(
                intent.build_request, now=issued + timedelta(minutes=30)
            )
        changed = StandardAuthorizationInputs(
            intent, _identity("other-index"), issued
        ).authorize()
        self.assertNotEqual(changed.identity, authorization.identity)

    def test_local_port_revalidates_sdk_before_constructing_authorization(self):
        with tempfile.TemporaryDirectory() as scratch:
            ports, _, _, intent = _python_copy_lifecycle(Path(scratch))
            with (
                patch.object(
                    ports,
                    "_require_intent_sdk_inputs",
                    side_effect=RuntimeError("SDK changed"),
                ),
                patch.object(StandardAuthorizationInputs, "authorize") as construct,
            ):
                with self.assertRaisesRegex(RuntimeError, "SDK changed"):
                    ports.authorize(intent, _identity("index"))
                construct.assert_not_called()

    def test_local_port_uses_controller_clock_and_records_both_documents(self):
        with tempfile.TemporaryDirectory() as scratch:
            ports, _, _, intent = _python_copy_lifecycle(Path(scratch))
            issued = datetime(2026, 9, 30, tzinfo=UTC)
            with (
                patch.object(ports, "clock", return_value=issued),
                patch.object(ports, "_evidence_recorder", object()),
                patch.object(ports, "_record_evidence") as record,
            ):
                authorization = ports.authorize(intent, _identity("index"))
            self.assertEqual(authorization.grant.issued_at, issued)
            self.assertEqual(
                [call.args[0] for call in record.call_args_list],
                [authorization.grant.to_dict(), authorization.to_dict()],
            )

    def test_returned_authorization_cannot_renew_controller_time(self):
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as scratch:
            ports, _, _, intent = _python_copy_lifecycle(Path(scratch))
            issued = datetime.now(UTC)
            with patch.object(ports, "clock", return_value=issued):
                inputs = ports.authorization_inputs(intent, _identity("index"))
            expected = inputs.authorize()
            renewed = replace(
                inputs, issued_at=issued + timedelta(minutes=1)
            ).authorize()
            with patch.object(ports, "_record_evidence") as record:
                with self.assertRaisesRegex(RuntimeError, "admitted controller inputs"):
                    ports.accept_build_authorization(inputs, renewed)
                with patch.object(
                    ports, "clock", return_value=issued + timedelta(minutes=30)
                ):
                    with self.assertRaises(AuthorizationError):
                        ports.accept_build_authorization(inputs, expected)
                record.assert_not_called()
