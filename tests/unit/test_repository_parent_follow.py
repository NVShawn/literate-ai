"""Parent-selector follow helpers stay deterministic without Git."""

from __future__ import annotations

import io
import unittest

from literate_ai.application.repository_parent_follow import (
    highest_release_tag,
    is_exact_git_object_id,
    next_release_line,
    parent_selector_kind,
    parse_release_line,
    parse_release_tag,
    release_lines,
)
from literate_ai.diagnostics import progress_reporting, report_progress


class RepositoryParentFollowTests(unittest.TestCase):
    def test_exact_commit_kind(self) -> None:
        sha = "66dd48ef0bcf1f3cea1bb9b6a843a389a5115b45"
        self.assertTrue(is_exact_git_object_id(sha))
        self.assertEqual(parent_selector_kind(sha), "exact-commit")
        self.assertEqual(parent_selector_kind("HEAD"), "moving-ref")
        self.assertEqual(parent_selector_kind("release/0.5.x"), "moving-ref")

    def test_next_release_line_from_current_line(self) -> None:
        heads = ("main", "release/0.3.x", "release/0.5.x", "release/0.4.x")
        self.assertEqual(
            release_lines(heads),
            ("release/0.3.x", "release/0.4.x", "release/0.5.x"),
        )
        self.assertEqual(next_release_line("release/0.3.x", heads), "release/0.4.x")
        self.assertIsNone(next_release_line("release/0.5.x", heads))
        self.assertIsNone(next_release_line("HEAD", heads))

    def test_sha_pin_jumps_to_latest_release_line(self) -> None:
        heads = ("release/0.3.x", "release/0.5.x")
        self.assertEqual(
            next_release_line("a" * 40, heads),
            "release/0.5.x",
        )
        self.assertIsNone(next_release_line("a" * 40, ("main",)))

    def test_parse_release_line(self) -> None:
        self.assertEqual(parse_release_line("release/0.5.x"), (0, 5))
        self.assertIsNone(parse_release_line("release/0.5.2"))
        self.assertIsNone(parse_release_line("main"))

    def test_highest_release_tag_ignores_moving_tips_and_pre_releases(self) -> None:
        tags = (
            "HEAD",
            "v0.7.2",
            "v0.8.0-rc.1",
            "v0.1.1a1",
            "v0.8.0",
            "v9.9.9",
            "not-a-release",
        )
        self.assertEqual(parse_release_tag("v0.7.2"), (0, 7, 2))
        self.assertIsNone(parse_release_tag("v0.8.0-rc.1"))
        self.assertEqual(highest_release_tag(tags), "v9.9.9")
        self.assertEqual(highest_release_tag(tags, at_most="0.8.0"), "v0.8.0")
        self.assertEqual(highest_release_tag(tags, at_most="v0.7.2"), "v0.7.2")
        self.assertIsNone(highest_release_tag(("HEAD", "main", "v0.8.0-rc.1")))
        self.assertIsNone(highest_release_tag(tags, at_most="not-a-version"))


class ProgressReportingTests(unittest.TestCase):
    def test_bound_stream_receives_stage_lines(self) -> None:
        stream = io.StringIO()
        with progress_reporting(stream):
            report_progress("Fetching example#HEAD")
        self.assertEqual(stream.getvalue(), "Literate AI: Fetching example#HEAD\n")


if __name__ == "__main__":
    unittest.main()
