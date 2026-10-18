"""Retained release review cannot select an unrelated or changed policy."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import retained_project_release as release
from literate_ai.adapters.retained_project_inputs import (
    discover_retained_project_inputs,
)
from literate_ai.adapters.retained_project_metadata import (
    read_retained_project_document,
)
from literate_ai.contracts import ContentIdentity, canonical_identity
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.project_releases import ReleasePolicy


class RetainedProjectReleasePolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        repository = Path(__file__).resolve().parents[2]
        (self.root / "literate.project.json").write_bytes(
            (repository / "literate.project.json").read_bytes()
        )
        self.path = self.root / "literate.release.json"
        self.path.write_bytes((repository / "literate.release.json").read_bytes())
        self.capture()

    def capture(self, path="literate.release.json"):
        self.manifest = discover_retained_project_inputs(
            self.root, {path: "release-policy"}, RetainedProjectLimits(10, 65536, 65536)
        )
        self.expected = ContentIdentity.parse_uri(
            "sha256:" + self.manifest.members[0].sha256
        )

    def read(self):
        return release.read_retained_project_release_policy(
            self.root, self.manifest, self.expected
        )

    def test_current_reviewed_policy_uses_existing_release_owner(self):
        expected = ReleasePolicy.from_dict(json.loads(self.path.read_bytes()))
        self.assertEqual(self.read(), replace(expected, default_branch="main"))

    def test_conflicting_repository_release_branch_refuses(self):
        policy = json.loads(self.path.read_bytes())
        policy["default_branch"] = "different"
        self.path.write_text(json.dumps(policy))
        self.capture()
        with self.assertRaisesRegex(ValueError, "default_branch differs"):
            self.read()

    def test_another_hashed_policy_is_not_configured_release_authority(self):
        (self.root / "alternative.json").write_bytes(self.path.read_bytes())
        self.capture("alternative.json")
        with self.assertRaisesRegex(ValueError, "configured-policy-input-required"):
            self.read()

    def test_stale_reviewed_bytes_or_identity_refuse(self):
        self.expected = canonical_identity("different policy")
        with self.assertRaisesRegex(ValueError, "configured-policy-input-required"):
            self.read()
        self.capture()
        self.path.write_bytes(self.path.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "document-changed"):
            self.read()

    def test_duplicate_keys_and_invalid_release_policy_refuse(self):
        for content in (b'{"schema":"one","schema":"two"}', b"{}"):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                self.capture()
                with self.assertRaises(ValueError):
                    self.read()

    def test_current_owner_policy_substitution_refuses(self):
        original = ReleasePolicy.from_dict(json.loads(self.path.read_bytes()))
        with (
            patch.object(
                release,
                "load_release_policy",
                return_value=(
                    self.root,
                    replace(original, signed_tag=not original.signed_tag),
                ),
            ),
            self.assertRaisesRegex(ValueError, "configured-policy-mismatch"),
        ):
            self.read()

    def test_change_during_existing_owner_read_is_detected(self):
        load = release.load_release_policy

        def changed(root):
            result = load(root)
            self.path.write_bytes(self.path.read_bytes() + b"\n")
            return result

        with (
            patch.object(release, "load_release_policy", side_effect=changed),
            self.assertRaisesRegex(ValueError, "document-changed"),
        ):
            self.read()

    def test_metadata_reader_keeps_policy_budget(self):
        with self.assertRaises(ValueError):
            read_retained_project_document(
                self.root, self.manifest.members[0], maximum_bytes=64
            )
