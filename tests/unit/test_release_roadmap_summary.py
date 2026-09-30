"""A deterministic queue summary cannot turn prose or missing work into a pass."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.summarize_release_roadmap import summarize


class ReleaseRoadmapSummaryTests(unittest.TestCase):
    def test_exact_release_line_preserves_unchecked_acceptance(self) -> None:
        content = """### [ ] AUD-ONE — first
- **Release target:** 1.1.0
  - [x] Implemented
  - [ ] Qualify installed artifact
### [x] AUD-TWO — historical
- **Release target:** 1.10
  - [x] Done
"""
        report = summarize(content, "1.1")
        self.assertEqual([item["id"] for item in report["items"]], ["AUD-ONE"])
        self.assertEqual(
            report["items"][0]["remaining"], ["Qualify installed artifact"]
        )
        self.assertFalse(report["ready"])
        self.assertEqual(report, summarize(content, "1.1.0"))

    def test_empty_duplicate_and_false_closure_do_not_pass(self) -> None:
        self.assertFalse(summarize("", "1.1")["ready"])
        content = (
            "### [x] AUD-ONE — first\n- **Release target:** 1.1\n  - [ ] Pending\n"
        )
        with self.assertRaises(ValueError):
            summarize(content, "1.1")
        content = content.replace("[x]", "[ ]")
        with self.assertRaises(ValueError):
            summarize(content + content, "1.1")

    def test_full_integration_refuses_untargeted_open_or_partial_items(self) -> None:
        for state in (" ", "~"):
            for target in ("", "- **Release target:** 1.10.0\n"):
                with self.subTest(state=state, target=target):
                    content = f"### [{state}] AUD-ONE — pending\n{target}"
                    with self.assertRaisesRegex(ValueError, "not targeted to 1.1"):
                        summarize(content, "1.1", require_all_open_targeted=True)

    def test_full_integration_refuses_hidden_unchecked_acceptance(self) -> None:
        content = "### [x] AUD-ONE — historical\n  - [ ] Missing proof\n"
        with self.assertRaisesRegex(ValueError, "AUD-ONE"):
            summarize(content, "1.1", require_all_open_targeted=True)

    def test_full_integration_keeps_completed_history_outside_current_scope(
        self,
    ) -> None:
        content = (
            "### [x] AUD-OLD — historical\n  - [x] Proven\n"
            "### [ ] AUD-NOW — current\n- **Release target:** 1.1.0\n"
        )
        self.assertEqual(
            summarize(content, "1.1", require_all_open_targeted=True),
            summarize(content, "1.1"),
        )

    def test_independent_checklists_do_not_reopen_a_closed_work_item(self) -> None:
        for boundary in (
            "# Appendix",
            "## PR landing queue",
            "### Other work",
            "   ## PRs",
        ):
            for target in ("", "- **Release target:** 1.1.0\n"):
                with self.subTest(boundary=boundary, target=target):
                    owned = f"### [x] AUD-OLD — done\n{target}  - [x] Proven\n"
                    independent = f"{boundary}\n  - [ ] Review open PR\n"
                    current = "### [ ] AUD-NOW — current\n- **Release target:** 1.1.0\n"
                    self.assertEqual(
                        summarize(
                            owned + independent + current,
                            "1.1",
                            require_all_open_targeted=True,
                        ),
                        summarize(
                            owned + current, "1.1", require_all_open_targeted=True
                        ),
                    )

    def test_release_target_in_another_section_cannot_target_an_open_item(self) -> None:
        content = (
            "### [ ] AUD-ONE — missing target\n"
            "## Other section\n- **Release target:** 1.1.0\n"
        )
        with self.assertRaisesRegex(ValueError, "not targeted to 1.1: AUD-ONE"):
            summarize(content, "1.1", require_all_open_targeted=True)

    def test_nested_acceptance_stays_with_its_owner_and_blocks_false_closure(
        self,
    ) -> None:
        for nested in (
            "#### Acceptance",
            "##### Evidence",
            "###### Final checks",
            "###not a heading",
        ):
            with self.subTest(nested=nested):
                content = (
                    "### [ ] AUD-ONE — current\n- **Release target:** 1.1.0\n"
                    f"{nested}\n  - [ ] Run `installed-check`\n"
                    "## Separate section\n  - [ ] Review open PR\n"
                )
                report = summarize(content, "1.1", require_all_open_targeted=True)
                self.assertEqual(
                    report["items"][0]["remaining"], ["Run `installed-check`"]
                )
                self.assertFalse(report["ready"])
                with self.assertRaisesRegex(
                    ValueError, "closed item has unchecked acceptance"
                ):
                    summarize(content.replace("### [ ]", "### [x]", 1), "1.1")

    def test_example_headings_do_not_split_real_acceptance_or_add_fake_items(
        self,
    ) -> None:
        for example in (
            "```markdown\n## Example\n### [ ] AUD-FAKE — example\n```",
            "~~~markdown\n## Example\n### [ ] AUD-FAKE — example\n~~~",
            "<!--\n## Example\n### [ ] AUD-FAKE — example\n-->",
        ):
            with self.subTest(example=example):
                content = (
                    "### [ ] AUD-ONE — current\n- **Release target:** 1.1.0\n"
                    f"{example}\n  - [ ] Qualify real artifact\n"
                )
                report = summarize(content, "1.1", require_all_open_targeted=True)
                self.assertEqual([item["id"] for item in report["items"]], ["AUD-ONE"])
                self.assertEqual(
                    report["items"][0]["remaining"], ["Qualify real artifact"]
                )

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
