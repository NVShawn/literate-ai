"""Pure generation transition helpers retained beside canonical lock CLI tests."""

from __future__ import annotations

import unittest
from unittest import mock

import literate_ai.cli.generation as generation_cli


class GenerationTransitionHelperTests(unittest.TestCase):
    def test_generation_stops_before_coding_cli_when_authority_is_unreviewed(
        self,
    ) -> None:
        prepared = object()
        failure = generation_cli.CliFailure(
            "project.documentation_authority_review_stale",
            "authority review is stale",
        )
        with (
            mock.patch.object(
                generation_cli, "_prepare_generation", return_value=prepared
            ),
            mock.patch.object(
                generation_cli,
                "_require_reviewed_component_authority",
                side_effect=failure,
            ) as authority_gate,
            mock.patch.object(
                generation_cli.FilesystemStandardSourceGenerationAdapter,
                "from_environment",
            ) as generator,
        ):
            with self.assertRaises(generation_cli.CliFailure) as raised:
                generation_cli.generate_from_args(object())

        self.assertEqual(
            raised.exception.code, "project.documentation_authority_review_stale"
        )
        authority_gate.assert_called_once_with(prepared)
        generator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
