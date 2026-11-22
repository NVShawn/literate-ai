from __future__ import annotations

import unittest

from literate_ai.project_releases import (
    ProjectReleaseError,
    ReleaseContributionsPolicy,
)


class ReleaseContributionsPolicyTests(unittest.TestCase):
    def test_optional_manifest_round_trip(self) -> None:
        for manifest in (None, "docs/roadmap/release-goals.json"):
            with self.subTest(manifest=manifest):
                policy = ReleaseContributionsPolicy("v{version}", manifest)
                serialized = policy.to_dict()
                original = dict(serialized)
                self.assertEqual(
                    ReleaseContributionsPolicy.from_dict(
                        serialized, path="release_policy.contributions"
                    ),
                    policy,
                )
                self.assertEqual(serialized, original)

    def test_explicit_null_normalizes_to_omission(self) -> None:
        raw = {"current_milestone": "1.4.0", "roadmap_goal_manifest": None}
        policy = ReleaseContributionsPolicy.from_dict(
            raw, path="release_policy.contributions"
        )
        self.assertEqual(policy.to_dict(), {"current_milestone": "1.4.0"})
        self.assertIn("roadmap_goal_manifest", raw)

    def test_required_milestone_and_unknown_keys_remain_rejected(self) -> None:
        for raw in (
            {},
            {"roadmap_goal_manifest": None},
            {"current_milestone": "1.4.0", "unknown": True},
            {"current_milestone": "1.4.0", "roadmap_goal_manfiest": "goals.json"},
            {"current_milestone": "{unknown}"},
            {"current_milestone": "{version}{version}"},
            {"current_milestone": None},
        ):
            with self.subTest(raw=raw), self.assertRaises(ProjectReleaseError):
                ReleaseContributionsPolicy.from_dict(
                    raw, path="release_policy.contributions"
                )

    def test_invalid_manifest_values_remain_rejected(self) -> None:
        for manifest in ("", "../goals.json", "/goals.json", 42, [], {}):
            with (
                self.subTest(manifest=manifest),
                self.assertRaises(ProjectReleaseError),
            ):
                ReleaseContributionsPolicy.from_dict(
                    {
                        "current_milestone": "1.4.0",
                        "roadmap_goal_manifest": manifest,
                    },
                    path="release_policy.contributions",
                )
