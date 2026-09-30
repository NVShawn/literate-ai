"""Contribution sweeps honor the project's milestone independently of its version."""

import argparse
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.cli.errors import CliFailure
from literate_ai.cli.release import release_from_args
from literate_ai.project_releases import ProjectReleaseError, ReleaseContributionsPolicy


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

    def test_configured_literal_and_template_apply_with_explicit_or_derived_version(
        self,
    ):
        for configured, expected in (
            ("1.1", "1.1"),
            ("release-{version}", "release-1.1.0"),
        ):
            for version in (None, "1.1.0"):
                with self.subTest(configured=configured, version=version):
                    policy = SimpleNamespace(
                        contributions=ReleaseContributionsPolicy(configured)
                    )
                    with (
                        patch(
                            "literate_ai.cli.release.load_release_policy",
                            return_value=(".", policy),
                        ),
                        patch(
                            "literate_ai.cli.release.current_release_version",
                            return_value="1.1.0",
                        ),
                        patch(
                            "literate_ai.release_contributions.sweep_release_contributions",
                            return_value={},
                        ) as sweep,
                    ):
                        self.assertEqual(
                            release_from_args(self.arguments(version=version)), ({}, 0)
                        )
                    self.assertEqual(sweep.call_args.kwargs["release"], "1.1.0")
                    self.assertEqual(
                        sweep.call_args.kwargs["current_milestone"], expected
                    )

    def test_explicit_milestone_override_needs_no_policy(self):
        with (
            patch("literate_ai.cli.release.load_release_policy") as load,
            patch(
                "literate_ai.release_contributions.sweep_release_contributions",
                return_value={},
            ) as sweep,
        ):
            release_from_args(self.arguments(current_milestone="operator-selected"))
        load.assert_not_called()
        self.assertEqual(
            sweep.call_args.kwargs["current_milestone"], "operator-selected"
        )

    def test_standalone_explicit_version_keeps_missing_policy_fallback(self):
        error = ProjectReleaseError("release.policy_unavailable", "missing")
        error.__cause__ = FileNotFoundError("missing policy")
        with (
            patch("literate_ai.cli.release.load_release_policy", side_effect=error),
            patch(
                "literate_ai.release_contributions.sweep_release_contributions",
                return_value={},
            ) as sweep,
        ):
            release_from_args(self.arguments())
        self.assertEqual(sweep.call_args.kwargs["current_milestone"], "1.1.0")

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

    def test_policy_supports_literal_names_and_rejects_ambiguous_templates(self):
        for value in ("1.1", "release-{version}"):
            policy = ReleaseContributionsPolicy.from_dict(
                {"current_milestone": value}, path="contributions"
            )
            self.assertEqual(policy.current_milestone, value)
        for value in ("", "{minor}", "{version}-{version}", "oops}"):
            with self.assertRaises(ProjectReleaseError):
                ReleaseContributionsPolicy.from_dict(
                    {"current_milestone": value}, path="contributions"
                )
