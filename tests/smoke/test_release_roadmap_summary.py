"""A deterministic queue summary cannot turn prose or missing work into a pass."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.summarize_release_roadmap import summarize


class ReleaseRoadmapSummaryTests(unittest.TestCase):
    def test_full_integration_refuses_untargeted_open_or_partial_items(self) -> None:
        for state in (" ", "~"):
            for target in ("", "- **Release target:** 1.10.0\n"):
                with self.subTest(state=state, target=target):
                    content = f"### [{state}] AUD-ONE — pending\n{target}"
                    with self.assertRaisesRegex(ValueError, "not targeted to 1.1"):
                        summarize(content, "1.1", require_all_open_targeted=True)

    def test_cli_reads_crlf_roadmap_without_mutating_it_and_refuses_false_closure(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temporary:
            roadmap = Path(temporary) / "active-work.md"
            command = [
                sys.executable,
                str(root / "scripts/summarize_release_roadmap.py"),
                "1.1",
                "--roadmap",
                str(roadmap),
                "--require-all-open-targeted",
            ]
            for state, status in ((" ", 0), ("x", 2)):
                with self.subTest(state=state):
                    content = (
                        f"### [{state}] AUD-ONE — current\r\n"
                        "- **Release target:** 1.1.0\r\n"
                        "#### Acceptance\r\n  - [ ] Qualify `wheel`\r\n"
                        "## PR queue\r\n  - [ ] Review open PR\r\n"
                    ).encode()
                    roadmap.write_bytes(content)
                    result = subprocess.run(
                        command, capture_output=True, text=True, cwd=root
                    )
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertEqual(roadmap.read_bytes(), content)
                    if status == 0:
                        report = json.loads(result.stdout)
                        self.assertFalse(report["ready"])
                        self.assertEqual(
                            report["items"][0]["remaining"], ["Qualify `wheel`"]
                        )
                    else:
                        self.assertIn(
                            "closed item has unchecked acceptance", result.stderr
                        )
