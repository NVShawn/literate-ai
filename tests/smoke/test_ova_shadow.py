"""Side-effect-free OVA shadow projection and comparison coverage."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from literate_ai.compatibility.ova import (
    OvaShadowComparator,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ova"
REPOSITORY = Path(__file__).resolve().parents[2]


class OvaShadowComparatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.comparator = OvaShadowComparator()

    def test_executable_report_is_json_and_does_not_modify_inputs(self) -> None:
        fixture_files = tuple(sorted((FIXTURES / "valid").iterdir()))
        before = {
            path: (path.stat().st_mtime_ns, path.read_bytes()) for path in fixture_files
        }
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(REPOSITORY / "src")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "literate_ai.compatibility.ova",
                "--baseline",
                str(FIXTURES / "valid"),
                "--candidate",
                str(FIXTURES / "valid"),
            ],
            cwd=REPOSITORY,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["exact"])
        after = {
            path: (path.stat().st_mtime_ns, path.read_bytes()) for path in fixture_files
        }
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
