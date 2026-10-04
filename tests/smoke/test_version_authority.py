from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from literate_ai.cli import main
from literate_ai.version import DISTRIBUTION_VERSION, EXPECTED_RELEASE_TAG
from literate_ai.version_check import _git_state, check_versions


class VersionAuthorityTests(unittest.TestCase):
    def test_cli_version_check_covers_authority_and_both_catalogs(self) -> None:
        output = StringIO()
        errors = StringIO()
        status = main(
            ["version", "check", "--no-project"],
            stdout=output,
            stderr=errors,
        )
        self.assertEqual(status, 0, errors.getvalue())
        envelope = json.loads(output.getvalue())
        self.assertTrue(envelope["ok"])
        result = envelope["result"]
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["distribution"]["authority_version"], DISTRIBUTION_VERSION
        )
        self.assertEqual(set(result["schema_catalogs"]), {"v1", "v2"})
        self.assertEqual(result["schema_catalogs"]["v1"]["published"]["file_count"], 17)
        self.assertEqual(
            result["schema_catalogs"]["v2"]["release"], DISTRIBUTION_VERSION
        )

    def test_non_editable_metadata_mismatch_remains_strict(self) -> None:
        with (
            patch(
                "literate_ai.version_check._distribution_metadata_version",
                return_value="0.8.3",
            ),
            patch(
                "literate_ai.version_check._distribution_is_editable",
                return_value=False,
            ),
        ):
            report = check_versions(project_path=None)

        self.assertFalse(report["ok"])
        self.assertFalse(report["distribution"]["ok"])
        self.assertIsNone(report["distribution"]["diagnostic"])

    def test_release_tag_is_discovered_from_nested_and_linked_worktrees(self) -> None:
        def git(root: Path, *args: str) -> None:
            subprocess.run(
                ["git", "-C", str(root), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )

        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            repository = parent / "repository"
            repository.mkdir()
            git(repository, "init")
            git(repository, "config", "user.email", "version-test@example.invalid")
            git(repository, "config", "user.name", "Version Test")
            (repository / "authority.txt").write_text("v0.2.0\n", encoding="utf-8")
            git(repository, "add", "authority.txt")
            git(repository, "commit", "-m", "version fixture")
            self.assertFalse(
                _git_state(repository, require_release_tag=True)["ok"],
                "release mode must require the exact version tag",
            )
            git(repository, "tag", EXPECTED_RELEASE_TAG)

            nested = repository / "projects" / "nested"
            nested.mkdir(parents=True)
            self.assertEqual(
                _git_state(nested, require_release_tag=True)["state"], "released"
            )

            linked = parent / "linked-worktree"
            git(repository, "worktree", "add", "--detach", str(linked), "HEAD")
            linked_nested = linked / "projects" / "nested"
            linked_nested.mkdir(parents=True)
            linked_report = _git_state(linked_nested, require_release_tag=True)
            self.assertEqual(linked_report["state"], "released")
            self.assertTrue(linked_report["ok"])


if __name__ == "__main__":
    unittest.main()
