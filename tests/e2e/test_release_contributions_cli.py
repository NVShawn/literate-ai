"""Contribution sweeps honor the project's milestone independently of its version."""

import argparse
import unittest
from pathlib import Path
from types import SimpleNamespace
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
                release_from_args(
                    self.arguments(current_milestone="explicit-milestone")
                )
        sweep.assert_not_called()

    def test_explicit_scope_still_uses_policy_goal_manifest(self):
        contributions = SimpleNamespace(
            current_milestone="configured-{version}",
            roadmap_goal_manifest="docs/roadmap/release-goals.json",
        )
        policy = SimpleNamespace(contributions=contributions)
        with (
            patch(
                "literate_ai.cli.release.load_release_policy",
                return_value=(Path("."), policy),
            ) as load_policy,
            patch(
                "literate_ai.release_contributions.sweep_release_contributions",
                return_value={"ready": False},
            ) as sweep,
        ):
            result, status = release_from_args(
                self.arguments(current_milestone="explicit-milestone")
            )

        self.assertEqual(status, 0)
        self.assertEqual(result, {"ready": False})
        load_policy.assert_called_once_with(Path("."))
        sweep.assert_called_once_with(
            Path("."),
            release="1.1.0",
            current_milestone="explicit-milestone",
            remote="origin",
            default_branch="main",
            roadmap_goal_manifest="docs/roadmap/release-goals.json",
        )

    def test_explicit_version_remains_available_without_policy(self):
        error = ProjectReleaseError("release.policy_unavailable", "missing")
        error.__cause__ = FileNotFoundError("literate.release.json")
        with (
            patch("literate_ai.cli.release.load_release_policy", side_effect=error),
            patch(
                "literate_ai.release_contributions.sweep_release_contributions",
                return_value={"ready": True},
            ) as sweep,
        ):
            result, status = release_from_args(
                self.arguments(current_milestone="explicit-milestone")
            )

        self.assertEqual(status, 0)
        self.assertEqual(result, {"ready": True})
        sweep.assert_called_once_with(
            Path("."),
            release="1.1.0",
            current_milestone="explicit-milestone",
            remote="origin",
            default_branch="main",
            roadmap_goal_manifest=None,
        )
