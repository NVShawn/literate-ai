"""Current verifier expectations come from guarded profile files, not archives."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

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
