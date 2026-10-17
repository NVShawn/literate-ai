"""Release entry points cannot use non-SemVer policy, including legacy documents."""

import json
import tempfile
import unittest
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import literate_ai.project_releases as releases
import tests.support.release_fixture as fixture_module
from literate_ai.contracts import canonical_identity


class ReleaseSemverRequirementTests(unittest.TestCase):
    def test_legacy_and_current_policy_are_readable_but_cannot_release(self):
        helper = fixture_module.ProjectReleaseTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        for schema in ("literate-ai/release-policy@1", "literate-ai/release-policy@2"):
            with (
                self.subTest(schema=schema),
                tempfile.TemporaryDirectory() as directory,
            ):
                parent = Path(directory)
                root, remote = helper.initialize(parent)
                policy_path = root / "literate.release.json"
                policy = json.loads(policy_path.read_text())
                policy.update(schema=schema, version_scheme="pep440")
                policy_path.write_text(json.dumps(policy))
                helper.git(root, "add", ".")
                helper.git(root, "commit", "-m", "Historical PEP 440 policy")
                refs = helper.git(remote, "show-ref")
                files = {
                    name: (root / name).read_bytes()
                    for name in helper.git(root, "ls-files").splitlines()
                }
                plan_path, prepared_path = (
                    parent / "plan.json",
                    parent / "prepared.json",
                )
                for path, record_schema in (
                    (plan_path, releases.RELEASE_PLAN_SCHEMA),
                    (prepared_path, releases.LEGACY_PREPARED_RELEASE_SCHEMA),
                ):
                    record = {"schema": record_schema}
                    record["identity"] = canonical_identity(record).uri
                    helper.write_record(path, record)
                with patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=SimpleNamespace(root=root),
                ):
                    self.assertEqual(
                        releases.load_release_policy(root)[1].version_scheme, "pep440"
                    )
                    operations = {
                        "plan": partial(
                            releases.create_release_plan,
                            root,
                            transition="patch",
                            explicit_version=None,
                        ),
                        "prepare": partial(releases.prepare_release, root, plan_path),
                        "check": partial(
                            releases.check_release,
                            root,
                            plan_path,
                            output=parent / "checked.json",
                        ),
                        "publish": partial(
                            releases.publish_release,
                            root,
                            prepared_path,
                            authorize_external_write=True,
                        ),
                        "verify": partial(
                            releases.verify_published_release, root, prepared_path
                        ),
                        "rc": partial(
                            releases.create_release_candidate,
                            root,
                            version="1.2.4rc1",
                            actor=None,
                            authorize_external_write=True,
                        ),
                        "advance": partial(
                            releases.advance_default_branch_version,
                            root,
                            branch="main",
                            transition="patch",
                            explicit_version=None,
                        ),
                        "merge": partial(
                            releases.merge_release_pull_request,
                            root,
                            number=1,
                            actor=None,
                            authorize_external_write=True,
                        ),
                    }
                    with patch(
                        "literate_ai.project_releases._authorize",
                        side_effect=AssertionError("authorization reached"),
                    ):
                        for name, operation in operations.items():
                            with (
                                self.subTest(operation=name),
                                self.assertRaises(
                                    releases.ProjectReleaseError
                                ) as error,
                            ):
                                operation()
                            self.assertEqual(
                                error.exception.code, "release.semver_required"
                            )
                self.assertEqual(helper.git(remote, "show-ref"), refs)
                self.assertEqual(helper.git(root, "tag", "--list"), "")
                self.assertFalse((parent / "checked.json").exists())
                for name, content in files.items():
                    self.assertEqual((root / name).read_bytes(), content, name)
