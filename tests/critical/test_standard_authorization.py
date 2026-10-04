"""Portable authorization preserves admitted intent and controller policy."""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from literate_ai.security import AuthorizationError
from tests.support.fixtures_test_standard_local_command_adapter import (
    _identity,
    _python_copy_lifecycle,
)


class StandardAuthorizationTests(unittest.TestCase):
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
