"""Contribution sweeps honor the project's milestone independently of its version."""

import argparse
import unittest
from unittest.mock import patch

from literate_ai.cli.errors import CliFailure
from literate_ai.cli.release import release_from_args
from literate_ai.project_releases import ProjectReleaseError


class ReleaseContributionCliTests(unittest.TestCase):
    def arguments(self, **overrides):
        values = dict(
            release_command="contributions",
            contributions_command="sweep",
            project=".",
            version="1.1.0",
            current_milestone=None,
            remote="origin",
            default_branch="main",
            require_ready=False,
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_malformed_policy_never_silently_changes_scope(self):
        error = ProjectReleaseError("release.policy_unavailable", "malformed")
        error.__cause__ = ValueError("invalid JSON")
        with (
            patch("literate_ai.cli.release.load_release_policy", side_effect=error),
            patch(
                "literate_ai.release_contributions.sweep_release_contributions"
            ) as sweep,
        ):
            with self.assertRaises(CliFailure):
                release_from_args(self.arguments())
        sweep.assert_not_called()
