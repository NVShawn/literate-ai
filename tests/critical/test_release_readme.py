"""Publication metadata and real Git readback controls for README verification."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import release_readme as readme
from literate_ai.project_releases import ProjectReleaseError, ReleasePolicy

REPOSITORY = "owner/project"


def provider(tag="v1.4.0", *, prerelease=False):
    return {
        "tag_name": tag,
        "name": "Release " + tag,
        "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{tag}",
        "draft": False,
        "prerelease": prerelease,
        "assets": [
            {
                "id": 42,
                "name": "package.whl",
                "size": 3,
                "state": "uploaded",
                "digest": "sha256:" + hashlib.sha256(b"abc").hexdigest(),
                "browser_download_url": (
                    f"https://github.com/{REPOSITORY}/releases/download/{tag}/package.whl"
                ),
            }
        ],
    }


class ReleaseReadmeTests(unittest.TestCase):
    def setUp(self):
        self.policy = readme.ReadmeLatestReleasePolicy("latest-release")
        self.provider = provider()
        with patch.object(readme, "_github_json", return_value=self.provider):
            self.release = readme.published_readme_release(
                Path.cwd(),
                REPOSITORY,
                tag="v1.4.0",
                prerelease=False,
                policy=self.policy,
            )
        self.section = readme.render_latest_release_section(self.policy, self.release)

    def validate(self, content):
        readme.validate_latest_release_readme(
            content,
            policy=self.policy,
            release=self.release,
        )

    def test_visible_section_and_crlf_are_accepted(self):
        self.validate("# Project\n\n" + self.section + "\n\n## History\nv1.2.0")
        self.validate(self.section.replace("\n", "\r\n"))
        self.validate(
            self.section + "\n\nThe 1.2.0 overview was published in place. "
            "For the current ETA, use the editable 1.3.1 briefing."
        )

    def test_stale_fields_markers_and_conflicting_claims_refuse(self):
        cases = [
            self.section.replace("Release v1.4.0", "Wrong title"),
            self.section.replace("v1.4.0", "v1.3.0"),
            self.section.replace("package.whl", "other.whl"),
            self.section.replace("| 3 |", "| 4 |"),
            self.section.replace("sha256:", "sha256:0"),
            self.section.replace(self.policy.begin, ""),
            self.section + self.policy.begin,
            self.policy.end + self.section.replace(self.policy.end, ""),
            self.section + "\n\nCurrent release: v1.3.0",
            self.section + "\n\nv1.3.0 (latest)",
            self.section + "\n\n## Current downloads\n\nv1.3.0",
            self.section
            + "\n\n## Latest release\n\n[old](https://github.com/owner/project/releases/tag/v1.3.0)",
        ]
        for content in cases:
            with (
                self.subTest(content=content),
                self.assertRaises(readme.ReleaseReadmeError) as caught,
            ):
                self.validate(content)
            self.assertEqual(caught.exception.code, "release.readme_latest_stale")
            self.assertIn("expected", str(caught.exception))
            self.assertIn("observed", str(caught.exception))

    def test_provider_state_and_metadata_refuse(self):
        for field, bad in [
            ("draft", True),
            ("prerelease", True),
            ("tag_name", "v1.3.0"),
            ("name", ""),
            ("html_url", "https://example.com"),
        ]:
            observed = copy.deepcopy(self.provider)
            observed[field] = bad
            with (
                self.subTest(field=field),
                patch.object(readme, "_github_json", return_value=observed),
                self.assertRaises(readme.ReleaseReadmeError),
            ):
                readme.published_readme_release(
                    Path.cwd(),
                    REPOSITORY,
                    tag="v1.4.0",
                    prerelease=False,
                    policy=self.policy,
                )
        for field, bad in [
            ("digest", "sha256:bad"),
            ("state", "new"),
            ("size", True),
            ("browser_download_url", "https://example.com/file"),
        ]:
            observed = copy.deepcopy(self.provider)
            observed["assets"][0][field] = bad
            with (
                self.subTest(field=field),
                patch.object(readme, "_github_json", return_value=observed),
                self.assertRaises(readme.ReleaseReadmeError),
            ):
                readme.published_readme_release(
                    Path.cwd(),
                    REPOSITORY,
                    tag="v1.4.0",
                    prerelease=False,
                    policy=self.policy,
                )

    def test_prerelease_checks_stable_unless_explicitly_opted_in(self):
        with patch.object(readme, "_github_json", return_value=self.provider) as api:
            stable = readme.published_readme_release(
                Path.cwd(),
                REPOSITORY,
                tag="v1.5.0rc1",
                prerelease=True,
                policy=self.policy,
            )
        self.assertTrue(api.call_args.args[1].endswith("/releases/latest"))
        self.assertEqual(stable, self.release)
        opted = readme.ReadmeLatestReleasePolicy("latest-release", True)
        with patch.object(
            readme, "_github_json", return_value=provider("v1.5.0rc1", prerelease=True)
        ) as api:
            candidate = readme.published_readme_release(
                Path.cwd(), REPOSITORY, tag="v1.5.0rc1", prerelease=True, policy=opted
            )
        self.assertTrue(api.call_args.args[1].endswith("/tags/v1.5.0rc1"))
        with self.assertRaises(readme.ReleaseReadmeError):
            readme.validate_latest_release_readme(
                readme.render_latest_release_section(opted, candidate),
                policy=self.policy,
                release=stable,
            )
        with patch.object(readme, "_github_json", return_value=None):
            absent = readme.published_readme_release(
                Path.cwd(),
                REPOSITORY,
                tag="v1.5.0rc1",
                prerelease=True,
                policy=self.policy,
            )
        self.assertIsNone(absent)
        readme.validate_latest_release_readme(
            readme.render_latest_release_section(self.policy, None),
            policy=self.policy,
            release=None,
        )

    def test_missing_digest_hashes_actual_download_and_refuses_truncation(self):
        asset = copy.deepcopy(self.provider["assets"][0])
        asset.pop("digest")

        def download(arguments, **kwargs):
            self.assertIn("repos/owner/project/releases/assets/42", arguments)
            self.assertIn("Accept: application/octet-stream", arguments)
            kwargs["stdout"].write(b"abc")
            return subprocess.CompletedProcess(arguments, 0, stderr=b"")

        with patch.object(readme, "run_with_tree_kill", side_effect=download):
            self.assertEqual(
                readme._asset_digest(Path.cwd(), REPOSITORY, asset),
                "sha256:" + hashlib.sha256(b"abc").hexdigest(),
            )
            asset["size"] = 4
            with self.assertRaises(readme.ReleaseReadmeError):
                readme._asset_digest(Path.cwd(), REPOSITORY, asset)

    def test_reads_real_remote_default_commit_even_with_stale_local_release_branch(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, local = root / "remote.git", root / "local"

            def git(*args):
                return (
                    subprocess.run(
                        ["git", *map(str, args)], check=True, capture_output=True
                    )
                    .stdout.decode()
                    .strip()
                )

            git("init", "--bare", remote)
            git("init", "-b", "main", local)
            git("-C", local, "config", "user.name", "Test")
            git("-C", local, "config", "user.email", "test@example.com")
            (local / "README.md").write_text(self.section)
            git("-C", local, "add", ".")
            git("-C", local, "commit", "-m", "Published README")
            revision = git("-C", local, "rev-parse", "HEAD")
            git("-C", local, "push", remote, "main")
            git("-C", local, "checkout", "-b", "release/1.4")
            (local / "README.md").write_text("stale local README")
            calls = []

            def api(root, endpoint, **kwargs):
                calls.append(endpoint)
                if endpoint.endswith("/releases/tags/v1.4.0"):
                    return self.provider
                if endpoint == "repos/owner/project":
                    return {"default_branch": "main"}
                if endpoint.endswith("/git/ref/heads/main"):
                    return {"object": {"type": "commit", "sha": revision}}
                self.assertEqual(
                    endpoint, f"repos/owner/project/contents/README.md?ref={revision}"
                )
                data = subprocess.run(
                    ["git", "--git-dir", str(remote), "show", f"{revision}:README.md"],
                    check=True,
                    capture_output=True,
                ).stdout
                return {
                    "type": "file",
                    "encoding": "base64",
                    "size": len(data),
                    "content": base64.b64encode(data).decode(),
                }

            with patch.object(readme, "_github_json", side_effect=api):
                readme.verify_remote_release_readme(
                    local,
                    REPOSITORY,
                    tag="v1.4.0",
                    prerelease=False,
                    policy=self.policy,
                )
            self.assertIn("/git/ref/heads/main", calls[2])
            self.assertEqual((local / "README.md").read_text(), "stale local README")

    def test_optional_policy_roundtrip_and_legacy_refusal(self):
        root = Path(__file__).resolve().parents[2]
        document = json.loads((root / "literate.release.json").read_text())
        policy = ReleasePolicy.from_dict(document)
        self.assertEqual(policy.readme_latest_release.marker, "litai-latest-release")
        self.assertEqual(ReleasePolicy.from_dict(policy.to_dict()), policy)
        document.pop("readme_latest_release")
        old = ReleasePolicy.from_dict(document)
        self.assertNotIn("readme_latest_release", old.to_dict())
        self.assertEqual(old.identity, ReleasePolicy.from_dict(old.to_dict()).identity)
        document["readme_latest_release"] = self.policy.to_dict()
        document["schema"] = "literate-ai/release-policy@1"
        with self.assertRaises(ProjectReleaseError):
            ReleasePolicy.from_dict(document)
