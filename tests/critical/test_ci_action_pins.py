"""Release CI must not change when a third-party action tag moves."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
USES = re.compile(r"(?m)^\s*(?:-\s*)?uses:\s*([^\n#]+)")
PIN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}")


class CiActionPinTests(unittest.TestCase):
    def test_all_external_actions_use_full_commit_ids(self) -> None:
        references = []
        for path in sorted((ROOT / ".github/workflows").glob("*.y*ml")):
            for match in USES.finditer(path.read_text()):
                reference = match[1].strip().strip("\"'")
                if reference.startswith("./"):
                    continue
                references.append(reference)
                with self.subTest(workflow=path.name, reference=reference):
                    self.assertRegex(reference, "^" + PIN.pattern + "$")
        self.assertTrue(references, "pin check must inspect actual action references")

    def test_policy_rejects_tags_short_hashes_and_expressions(self) -> None:
        for reference in (
            "actions/checkout@v6",
            "actions/checkout@abcdef0",
            "${{ matrix.action }}",
        ):
            self.assertIsNone(PIN.fullmatch(reference))
