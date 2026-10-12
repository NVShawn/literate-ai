"""Historical policies do not exempt releases from versioned branch ownership."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tests.support.release_fixture as fixtures
from literate_ai.contracts import canonical_identity
from literate_ai.project_releases import (
    ProjectReleaseError,
    check_release,
    create_release_plan,
    prepare_release,
    publish_release,
    verify_published_release,
)


class LegacyReleaseLineTests(unittest.TestCase):
    def test_policy_without_default_branch_requires_and_can_publish_matching_line(self):
        helper = fixtures.ProjectReleaseTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root, remote = helper.initialize(parent)
            self.assertNotIn(
                "default_branch",
                json.loads((root / "literate.release.json").read_text()),
            )
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                initial_refs = helper.git(remote, "show-ref")
                with self.assertRaises(ProjectReleaseError) as error:
                    create_release_plan(root, transition="patch", explicit_version=None)
                self.assertEqual(
                    error.exception.code, "release.branch_not_release_line"
                )
                helper.git(root, "checkout", "-b", "release/1.2.x")
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                self.assertEqual(
                    plan["release_line"], {"name": "release/1.2.x", "create": False}
                )
                plan_path = parent / "plan.json"
                prepared_path = parent / "prepared.json"

                # An old plan with no release-line field cannot prepare on main.
                helper.git(root, "checkout", "main")
                historical = dict(plan, source_branch="main")
                historical.pop("release_line")
                historical["identity"] = canonical_identity(
                    {
                        key: value
                        for key, value in historical.items()
                        if key != "identity"
                    }
                ).uri
                helper.write_record(plan_path, historical)
                with self.assertRaises(ProjectReleaseError) as error:
                    prepare_release(root, plan_path)
                self.assertEqual(
                    error.exception.code, "release.branch_not_release_line"
                )
                self.assertEqual(helper.git(root, "status", "--porcelain"), "")
                self.assertEqual(helper.git(remote, "show-ref"), initial_refs)

                helper.git(root, "checkout", "release/1.2.x")
                helper.write_record(plan_path, plan)
                prepare_release(root, plan_path)
                helper.git(root, "add", ".")
                helper.git(root, "commit", "-m", "Prepare release on matching line")
                check_release(root, plan_path, output=prepared_path)
                published = publish_release(
                    root, prepared_path, authorize_external_write=True
                )
                self.assertEqual(
                    verify_published_release(root, prepared_path)["revision"],
                    published["revision"],
                )
                self.assertEqual(
                    helper.git(remote, "rev-parse", "refs/heads/release/1.2.x"),
                    published["revision"],
                )
                prepared = json.loads(prepared_path.read_text())
                for branch in ("main", "feature", "release/9.9.x"):
                    altered = dict(prepared, branch=branch)
                    altered["identity"] = canonical_identity(
                        {
                            key: value
                            for key, value in altered.items()
                            if key != "identity"
                        }
                    ).uri
                    helper.write_record(prepared_path, altered)
                    with (
                        self.subTest(branch=branch),
                        self.assertRaises(ProjectReleaseError) as error,
                    ):
                        verify_published_release(root, prepared_path)
                    self.assertEqual(
                        error.exception.code, "release.branch_not_release_line"
                    )
