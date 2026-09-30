"""Current verifier expectations come from guarded profile files, not archives."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters.qualification_authority import (
    read_current_qualification_authority,
)
from literate_ai.contracts import canonical_identity
from literate_ai.projects import PinnedInputClosureError
from literate_ai.source_to_specification.host_qualification import (
    LocalQualificationCase,
    LocalQualificationProfile,
)


class QualificationAuthorityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.path = self.root / "profile.json"
        self.source = canonical_identity({"reviewed-source-snapshot": 1})
        self.profile = LocalQualificationProfile(
            "retained-profile@1",
            ("build",),
            (("test",),),
            ("baseline",),
            ("generated",),
            (
                LocalQualificationCase("z", ({"value": 1},), {"value": 2}),
                LocalQualificationCase("a", ({"value": 2},), {"value": 3}),
            ),
            ("api.value",),
        )
        self.write(self.profile)

    def write(self, profile):
        self.path.write_text(json.dumps(profile.to_dict()) + "\n")

    def read(self, **kwargs):
        return read_current_qualification_authority(
            self.root,
            "profile.json",
            source_snapshot_identity=self.source,
            **kwargs,
        )

    def test_current_map_without_baseline_tree_or_execution(self):
        before = self.path.read_bytes()
        with mock.patch("subprocess.Popen", side_effect=AssertionError("execution")):
            current = self.read()
            current.require_unchanged()
        self.assertEqual(current.profile, self.profile)
        self.assertEqual(
            current.case_map.verifier_identity,
            canonical_identity(
                {
                    "provider": "filesystem-standard-independent-parity@1",
                    "source_snapshot_identity": self.source.uri,
                    "profile_identity": self.profile.identity,
                }
            ),
        )
        self.assertEqual([case.case_id for case in current.case_map.cases], ["a", "z"])
        self.assertEqual(list(self.root.iterdir()), [self.path])
        self.assertEqual(self.path.read_bytes(), before)

    def test_current_case_change_invalidates_guard_and_changes_verifier(self):
        first = self.read()
        self.write(
            replace(
                self.profile,
                cases=(LocalQualificationCase("a", ({"value": 2},), {"value": 4}),),
            )
        )
        with self.assertRaises(PinnedInputClosureError):
            first.require_unchanged()
        second = self.read()
        self.assertNotEqual(
            first.case_map.verifier_identity, second.case_map.verifier_identity
        )
        self.assertNotEqual(first.case_map.identity, second.case_map.identity)

    def test_duplicate_fields_and_limits_refuse(self):
        original = self.path.read_text()
        self.path.write_text('{"schema":"foreign",' + original[1:])
        with self.assertRaisesRegex(ValueError, "duplicate-field"):
            self.read()
        self.path.write_text(original)
        with self.assertRaises(PinnedInputClosureError):
            self.read(maximum_bytes=8)
        for maximum in (0, -1, True):
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                self.read(maximum_bytes=maximum)

    def test_source_identity_changes_verifier_and_unsafe_path_refuses(self):
        first = self.read()
        other = read_current_qualification_authority(
            self.root,
            "profile.json",
            source_snapshot_identity=canonical_identity(
                {"reviewed-source-snapshot": 2}
            ),
        )
        self.assertNotEqual(
            first.case_map.verifier_identity, other.case_map.verifier_identity
        )
        with self.assertRaises(ValueError):
            read_current_qualification_authority(
                self.root, "../profile.json", source_snapshot_identity=self.source
            )
