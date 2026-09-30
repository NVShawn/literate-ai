"""Acceptance-oracle tests, not a substitute for the installed-wheel journey."""

from __future__ import annotations

import copy
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.contracts.html_observability import HtmlStalenessReport
from literate_ai.contracts.identity import canonical_identity
from scripts.installed_html_smoke import (
    UPDATE_AUTHORITY,
    UPDATE_MARKER,
    prepare_update_parent,
    require_update,
    require_verdict,
)


class HtmlUpdateOracleTests(unittest.TestCase):
    def update(self, applied=True):
        return {
            "schema": "literate-ai/composite-project-update@1",
            "mode": "applied" if applied else "read-only-plan",
            "changed": True,
            "follow": {"from": "a" * 40, "to": "b" * 40},
            "repository_lineage": {"applied": {"state": "applied"}},
            "parent_selectors": [
                {"requested_revision": "b" * 40, "resolved_revision": "b" * 40}
            ],
        }

    def verdict(self, status="current"):
        expected = canonical_identity({"graph": "after"})
        observed = (
            expected if status == "current" else canonical_identity({"graph": "before"})
        )
        report = HtmlStalenessReport(
            "graph.html",
            status,
            expected,
            observed,
            ("authority-graph",) if status == "stale" else (),
        )
        return {
            "schema": "literate-ai/project-verify@1",
            "ok": status == "current",
            "gates": [
                {
                    "gate": "html-observability",
                    "state": "pass" if status == "current" else "fail",
                    "artifacts": [report.to_dict()],
                }
            ],
        }

    def test_exact_plan_and_applied_revision_pass(self):
        for applied in (False, True):
            require_update(self.update(applied), "a" * 40, "b" * 40, applied=applied)

    def test_noop_wrong_mode_or_unreviewed_revision_fails(self):
        original = self.update()
        for key, value in (
            ("schema", "literate-ai/project-update-noop@1"),
            ("mode", "read-only-plan"),
            ("changed", False),
            ("follow", {"from": "a" * 40, "to": "c" * 40}),
            ("follow", {"from": "c" * 40, "to": "b" * 40}),
            ("repository_lineage", {}),
            ("parent_selectors", []),
        ):
            with self.subTest(field=key), self.assertRaises(RuntimeError):
                require_update(
                    {**original, key: value}, "a" * 40, "b" * 40, applied=True
                )

    def test_selector_resolution_must_match_reviewed_commit(self):
        for field in ("requested_revision", "resolved_revision"):
            value = self.update()
            value["parent_selectors"][0][field] = "c" * 40
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                require_update(value, "a" * 40, "b" * 40, applied=True)

    def test_unchanged_parent_is_not_update_acceptance(self):
        value = self.update()
        value["follow"]["to"] = "a" * 40
        with self.assertRaises(RuntimeError):
            require_update(value, "a" * 40, "a" * 40, applied=True)

    def test_current_and_stale_reports_pass(self):
        for status in ("current", "stale"):
            value = self.verdict(status)
            self.assertEqual(
                require_verdict(value, status), value["gates"][0]["artifacts"][0]
            )

    def test_skipped_or_other_gate_is_not_html_acceptance(self):
        for field, value in (
            ("state", "skipped"),
            ("gate", "authority"),
            ("artifacts", []),
        ):
            result = self.verdict()
            result["gates"][0][field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                require_verdict(result, "current")

    def test_wrong_envelope_or_gate_count_fails(self):
        original = self.verdict()
        for field, value in (
            ("schema", "other"),
            ("ok", False),
            ("gates", []),
            ("gates", original["gates"] * 2),
        ):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                require_verdict({**original, field: value}, "current")

    def test_stale_renderer_does_not_prove_parent_source_changed(self):
        for labels in (["renderer"], ["authority-graph", "renderer"]):
            value = self.verdict("stale")
            value["gates"][0]["artifacts"][0]["stale_source_labels"] = labels
            with self.subTest(labels=labels), self.assertRaises(RuntimeError):
                require_verdict(value, "stale")

    def test_another_output_or_untyped_report_fails(self):
        original = self.verdict()
        for field, value in (
            ("artifact_path", "another.html"),
            ("unexpected", True),
            ("expected_render_inputs_identity", None),
        ):
            result = copy.deepcopy(original)
            result["gates"][0]["artifacts"][0][field] = value
            with (
                self.subTest(field=field),
                self.assertRaises((ValueError, RuntimeError)),
            ):
                require_verdict(result, "current")


class HtmlUpdateParentFixtureTests(unittest.TestCase):
    @mock.patch.dict(
        os.environ,
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.autocrlf",
            "GIT_CONFIG_VALUE_0": "true",
        },
    )
    def test_real_git_revision_changes_only_fixture_authority_and_keeps_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "source"
            source.mkdir()
            hooks = root / "hooks"
            hooks.mkdir()

            def git(*args, cwd=source):
                return subprocess.run(
                    (
                        "git",
                        "-c",
                        f"core.hooksPath={hooks}",
                        "-c",
                        "commit.gpgsign=false",
                        "-c",
                        "user.name=Fixture",
                        "-c",
                        "user.email=fixture@example.invalid",
                        *args,
                    ),
                    cwd=cwd,
                    check=True,
                    text=True,
                    capture_output=True,
                    timeout=120,
                ).stdout.strip()

            git("init", "-b", "main")
            authority = source / UPDATE_AUTHORITY
            authority.parent.mkdir(parents=True)
            authority.write_bytes(b"# Original authority\n")
            git("add", ".")
            git("commit", "-m", "Baseline")
            baseline = git("rev-parse", "HEAD")
            bare = root / "parent.git"
            git("clone", "--bare", "--no-local", str(source), str(bare))
            before, after, content = prepare_update_parent(bare, root / "update")
            self.assertEqual(before, baseline)
            self.assertNotEqual(after, baseline)
            self.assertEqual(content, b"# Original authority\n" + UPDATE_MARKER)
            self.assertEqual(git("--git-dir", str(bare), "rev-parse", "HEAD"), baseline)
            self.assertEqual(
                git("--git-dir", str(bare), "rev-parse", "html-acceptance"), after
            )
            self.assertEqual(
                git("--git-dir", str(bare), "diff", "--name-only", baseline, after),
                UPDATE_AUTHORITY,
            )
            self.assertEqual(authority.read_bytes(), b"# Original authority\n")

    def test_preexisting_workspace_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            marker = workspace / "keep"
            marker.write_bytes(b"user content")
            with self.assertRaises(FileExistsError):
                prepare_update_parent(root, workspace)
            self.assertEqual(marker.read_bytes(), b"user content")
