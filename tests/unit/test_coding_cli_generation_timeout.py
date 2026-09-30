"""LITERATE_AI_CODING_CLI_TIMEOUT_SECONDS configures local generation (#34)."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from literate_ai.adapters.models import CodingCliError
from literate_ai.adapters.standard_project import (
    CODING_CLI_GENERATION_TIMEOUT_ENVIRONMENT,
    _coding_cli_generation_timeout_seconds,
)


class CodingCliGenerationTimeoutTests(unittest.TestCase):
    def test_defaults_to_900_seconds_when_unset(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop(CODING_CLI_GENERATION_TIMEOUT_ENVIRONMENT, None)
            self.assertEqual(_coding_cli_generation_timeout_seconds(), 900)

    def test_honors_a_configured_positive_override(self) -> None:
        with mock.patch.dict(
            "os.environ", {CODING_CLI_GENERATION_TIMEOUT_ENVIRONMENT: "3600"}
        ):
            self.assertEqual(_coding_cli_generation_timeout_seconds(), 3600)

    def test_rejects_a_non_positive_or_non_integer_override(self) -> None:
        for invalid in ("0", "-5", "not-a-number", ""):
            with self.subTest(invalid=invalid):
                with mock.patch.dict(
                    "os.environ", {CODING_CLI_GENERATION_TIMEOUT_ENVIRONMENT: invalid}
                ):
                    with self.assertRaises(CodingCliError) as raised:
                        _coding_cli_generation_timeout_seconds()
                self.assertEqual(
                    raised.exception.code,
                    "coding_cli.generation_timeout_configuration_invalid",
                )


if __name__ == "__main__":
    unittest.main()
