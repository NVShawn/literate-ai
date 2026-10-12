"""Remote ref agreement cannot substitute policy-bound release names."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tests.support.release_fixture as fixture_module
from literate_ai.contracts import canonical_identity
from literate_ai.project_releases import (
    ProjectReleaseError,
    check_release,
    create_release_plan,
    prepare_release,
    publish_release,
    verify_published_release,
)


class PublishedReleaseIdentityTests(unittest.TestCase):
    def test_rehashed_record_and_matching_remote_refs_cannot_change_release_names(self):
        helper = fixture_module.ProjectReleaseTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root, remote = helper._initialize_with_default_branch(parent)
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                plan_path, prepared_path = (
                    parent / "plan.json",
                    parent / "prepared.json",
                )
                helper.write_record(plan_path, plan)
                prepare_release(root, plan_path)
                helper.git(root, "add", ".")
                helper.git(root, "commit", "-m", "Prepare release")
                check_release(root, plan_path, output=prepared_path)
                published = publish_release(
                    root, prepared_path, authorize_external_write=True
                )
                original = json.loads(prepared_path.read_text())
                self.assertEqual(
                    verify_published_release(root, prepared_path)["revision"],
                    published["revision"],
                )
                for branch in ("main", "feature", "release/9.9.x"):
                    helper.git(
                        remote,
                        "update-ref",
                        f"refs/heads/{branch}",
                        original["prepared_revision"],
                    )
                    altered = original | {"branch": branch}
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
                    self.assertIn(
                        error.exception.code,
                        {
                            "release.default_branch_cut_forbidden",
                            "release.branch_not_release_line",
                        },
                    )
                helper.git(
                    root,
                    "tag",
                    "-a",
                    "v9.9.9",
                    original["prepared_revision"],
                    "-m",
                    "Unrelated tag",
                )
                helper.git(root, "push", "origin", "refs/tags/v9.9.9")
                altered = original | {"tag": "v9.9.9"}
                altered["identity"] = canonical_identity(
                    {key: value for key, value in altered.items() if key != "identity"}
                ).uri
                helper.write_record(prepared_path, altered)
                with self.assertRaises(ProjectReleaseError) as error:
                    verify_published_release(root, prepared_path)
                self.assertEqual(error.exception.code, "release.prepared_tag_mismatch")
                self.assertEqual(
                    helper.git(remote, "rev-parse", "refs/tags/v9.9.9^{}"),
                    original["prepared_revision"],
                )

    def test_publish_refuses_substituted_tag_before_creating_or_pushing_refs(self):
        helper = fixture_module.ProjectReleaseTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root, remote = helper._initialize_with_default_branch(parent)
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                plan_path, prepared_path = (
                    parent / "plan.json",
                    parent / "prepared.json",
                )
                helper.write_record(plan_path, plan)
                prepare_release(root, plan_path)
                helper.git(root, "add", ".")
                helper.git(root, "commit", "-m", "Prepare release")
                check_release(root, plan_path, output=prepared_path)
                original = json.loads(prepared_path.read_text())
                before = helper.git(remote, "show-ref")
                for tag in ("v9.9.9", "unrelated-release"):
                    altered = original | {"tag": tag}
                    altered["identity"] = canonical_identity(
                        {
                            key: value
                            for key, value in altered.items()
                            if key != "identity"
                        }
                    ).uri
                    helper.write_record(prepared_path, altered)
                    with (
                        self.subTest(tag=tag),
                        self.assertRaises(ProjectReleaseError) as error,
                    ):
                        publish_release(
                            root, prepared_path, authorize_external_write=True
                        )
                    self.assertEqual(
                        error.exception.code, "release.prepared_tag_mismatch"
                    )
                    self.assertEqual(helper.git(root, "tag", "--list"), "")
                    self.assertEqual(helper.git(remote, "show-ref"), before)
                    self.assertEqual(helper.git(root, "status", "--porcelain"), "")
                helper.write_record(prepared_path, original)
                published = publish_release(
                    root, prepared_path, authorize_external_write=True
                )
                self.assertEqual(published["tag"], "v1.2.4")
                self.assertEqual(
                    verify_published_release(root, prepared_path)["revision"],
                    published["revision"],
                )
