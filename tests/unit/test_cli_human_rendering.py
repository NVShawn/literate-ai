"""Interactive rendering must summarize without becoming the contract.

The JSON envelope is what callers and CI parse. A human rendering exists only so a
terminal is readable, so two properties matter: piped output stays exactly the JSON
envelope, and the interactive text never omits a failure.
"""

from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from literate_ai.cli import main


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def render(command: str, result: dict[str, object]) -> str:
    output = TtyStringIO()
    errors = TtyStringIO()
    with patch(
        "literate_ai.cli.dispatch._handle",
        return_value=(result, 0),
    ):
        status = main((command,), stdout=output, stderr=errors)
    if status != 0:
        raise AssertionError(
            f"{command} exited {status}: stdout={output.getvalue()!r} "
            f"stderr={errors.getvalue()!r}"
        )
    if errors.getvalue():
        raise AssertionError(f"{command} wrote stderr: {errors.getvalue()!r}")
    return output.getvalue()


class HumanRendererTests(unittest.TestCase):
    def test_verify_shows_every_gate_including_failures(self) -> None:
        text = render(
            "verify",
            {
                "project": ".",
                "ok": False,
                "gates": [
                    {"gate": "authority", "state": "pass", "detail": "reviewed"},
                    {"gate": "locks", "state": "fail", "detail": "lock is stale"},
                    {"gate": "receipt", "state": "skipped", "detail": "no policy"},
                ],
            },
        )
        self.assertIn("FAILED", text)
        for gate in ("authority", "locks", "receipt"):
            self.assertIn(gate, text)
        self.assertIn("lock is stale", text)

    def test_build_names_how_to_run_the_result(self) -> None:
        text = render(
            "build",
            {
                "component": "samples/hello-component",
                "passed": True,
                "artifact": "/p/run.pyz",
                "run": "litai run hello",
                "test_summary": {"passed": 3, "total": 3},
            },
        )
        self.assertIn("passed", text)
        self.assertIn("litai run hello", text)

    def test_update_reports_conflicts_by_name(self) -> None:
        text = render(
            "update",
            {
                "mode": "read-only-plan",
                "apply_supported": False,
                "counts": {"unchanged": 5, "conflict": 1},
                "files": [{"path": "a/spec.md", "classification": "conflict"}],
            },
        )
        self.assertIn("a/spec.md", text)
        self.assertIn("--apply", text)

    def test_update_prints_ours_theirs_conflict_markers(self) -> None:
        text = render(
            "update",
            {
                "mode": "read-only-plan",
                "apply_supported": False,
                "counts": {"unchanged": 5, "conflict": 1},
                "files": [
                    {
                        "path": "a/spec.md",
                        "classification": "conflict",
                        "unified_diff": (
                            "--- a/spec.md\n"
                            "+++ a/spec.md\n"
                            "<<<<<<< ours\n"
                            "local overlay\n"
                            "=======\n"
                            "upstream catalog\n"
                            ">>>>>>> theirs\n"
                        ),
                    }
                ],
                "conflict_diffs": [
                    {
                        "path": "a/spec.md",
                        "ours": "local overlay\n",
                        "theirs": "upstream catalog\n",
                        "unified_diff": (
                            "--- a/spec.md\n"
                            "+++ a/spec.md\n"
                            "<<<<<<< ours\n"
                            "local overlay\n"
                            "=======\n"
                            "upstream catalog\n"
                            ">>>>>>> theirs\n"
                        ),
                    }
                ],
            },
        )
        self.assertIn("<<<<<<< ours", text)
        self.assertIn("local overlay", text)
        self.assertIn("upstream catalog", text)
        self.assertNotIn("unchanged file dump", text)

    def test_update_is_quiet_when_nothing_moved(self) -> None:
        text = render(
            "update",
            {"mode": "read-only-plan", "counts": {"unchanged": 60}, "files": []},
        )
        self.assertIn("Nothing upstream has moved", text)

    def test_composite_update_reports_nested_counts_and_pins(self) -> None:
        text = render(
            "update",
            {
                "schema": "literate-ai/composite-project-update@1",
                "mode": "read-only-plan",
                "apply_supported": True,
                "changed": True,
                "parent_selectors": [
                    {
                        "requested_revision": "a" * 40,
                        "resolved_revision": "a" * 40,
                        "kind": "exact-commit",
                        "pinned": True,
                    }
                ],
                "framework": {
                    "counts": {"unchanged": 4, "conflict": 1},
                    "files": [{"path": "SKILL.md", "classification": "conflict"}],
                },
                "repository_lineage": {"counts": {"unchanged": 12}, "files": []},
            },
        )
        self.assertIn("Parent is pinned to exact commit", text)
        self.assertIn("SKILL.md", text)
        self.assertIn("Framework templates:", text)
        self.assertIn("Inherited catalogs:", text)
        self.assertNotIn("Nothing upstream has moved", text)

    def test_composite_update_is_quiet_only_when_both_halves_are_still(self) -> None:
        text = render(
            "update",
            {
                "schema": "literate-ai/composite-project-update@1",
                "mode": "read-only-plan",
                "changed": False,
                "parent_selectors": [
                    {
                        "requested_revision": "HEAD",
                        "kind": "moving-ref",
                        "pinned": False,
                    }
                ],
                "framework": {"counts": {"unchanged": 8}, "files": []},
                "repository_lineage": {"counts": {"already-current": 3}, "files": []},
            },
        )
        self.assertIn("Nothing upstream has moved", text)
        self.assertNotIn("Parent is pinned", text)

    def test_run_reports_substituted_arguments(self) -> None:
        substituted = render(
            "run",
            {
                "component": "hello",
                "exit_status": 0,
                "arguments_source": "acceptance case 'primary'",
            },
        )
        self.assertIn("acceptance case", substituted)
        supplied = render(
            "run",
            {"component": "hello", "exit_status": 0, "arguments_source": "supplied"},
        )
        self.assertNotIn("acceptance case", supplied)

    def test_test_renderer_reports_current_summary(self) -> None:
        text = render(
            "test",
            {
                "component": "hello",
                "passed": True,
                "test_summary": {"passed": 4, "failed": 0, "skipped": 1},
            },
        )
        self.assertIn("hello passed", text)
        self.assertIn("4 passed", text)

    def test_piped_output_is_the_json_envelope(self) -> None:
        """Non-interactive callers must keep parsing exactly what they parsed before."""

        # Assert the envelope shape, not the verdict: whether this repository's
        # version check currently passes is not what this test is about.
        out, err = io.StringIO(), io.StringIO()
        main(("version", "check"), stdout=out, stderr=err)
        envelope = json.loads(out.getvalue() or err.getvalue())
        self.assertIn(
            envelope["schema"],
            ("literate-ai/cli-result@1", "literate-ai/cli-error@1"),
        )
        self.assertEqual(envelope["command"], "version.check")


if __name__ == "__main__":
    unittest.main()
