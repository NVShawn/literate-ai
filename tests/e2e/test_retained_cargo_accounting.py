"""Cargo selection accounting never turns excluded cases into passing evidence."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.harness_test_discovery import observe_test_collection
from tests.support.fixtures_test_retained_harness_receipts import (
    _adapter,
    _invoke,
    _legacy_project,
    _selectors,
)


class RetainedCargoAccountingTests(unittest.TestCase):
    def test_selected_and_excluded_counts_remain_distinct(self):
        output = (
            b"test result: ok. 2 passed; 0 failed; 3 ignored;\n"
            b"test result: ok. 1 passed; 0 failed; 4 ignored;\n"
        )
        observed = observe_test_collection("test", output, b"")
        self.assertEqual(observed["state"], "nonempty")
        self.assertEqual(observed["total"], 3)
        self.assertEqual(observed["passed"], 3)
        self.assertEqual(observed["excluded"], 7)
        self.assertEqual(observed["skipped"], 0)

    def test_ignored_only_suite_has_no_selected_tests(self):
        observed = observe_test_collection(
            "test", b"test result: ok. 0 passed; 0 failed; 3 ignored;", b""
        )
        self.assertEqual(observed["state"], "empty")
        self.assertEqual(observed["total"], 0)
        self.assertEqual(observed["excluded"], 3)

    def test_selected_failures_and_runtime_skips_remain_nonpassing(self):
        observed = observe_test_collection(
            "test", b"test result: FAILED. 2 passed; 1 failed; 3 ignored;", b""
        )
        self.assertEqual(observed["total"], 3)
        self.assertEqual(observed["failed"], 1)
        self.assertEqual(observed["excluded"], 3)
        observed = observe_test_collection("test", b"2 passed, 1 skipped", b"")
        self.assertEqual(observed["skipped"], 1)
        observed = observe_test_collection("test", b"Ran 3 tests\nOK (skipped=1)", b"")
        self.assertEqual(observed["skipped"], 1)

    @unittest.skipUnless(shutil.which("cargo"), "actual Cargo selection needs Rust")
    def test_actual_cargo_default_and_explicit_ignored_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Cargo.toml").write_text(
                '[package]\nname = "selection-fixture"\nversion = "0.1.0"\n'
                '[lib]\npath = "lib.rs"\n'
            )
            (root / "lib.rs").write_text(
                "#[test] fn selected() {}\n#[test] #[ignore] fn opt_in() {}\n"
            )
            for extra, selected, excluded in (
                ([], 1, 1),
                (["--", "--include-ignored"], 2, 0),
            ):
                result = subprocess.run(
                    ["cargo", "test", *extra], cwd=root, capture_output=True, timeout=60
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                observed = observe_test_collection("test", result.stdout, result.stderr)
                self.assertEqual(observed["total"], selected)
                self.assertEqual(observed["passed"], selected)
                self.assertEqual(observed["excluded"], excluded)

    def test_public_retained_receipt_admits_only_passing_selected_suite(self):
        summaries = (
            ("test result: ok. 2 passed; 0 failed; 1 ignored;", True),
            ("test result: FAILED. 1 passed; 1 failed; 1 ignored;", False),
            ("test result: ok. 0 passed; 0 failed; 2 ignored;", False),
            ("1 passed, 1 skipped", False),
        )
        for summary, accepted in summaries:
            with (
                self.subTest(summary=summary),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                project = root / "converted"
                _legacy_project(project)
                _adapter().initialize(
                    project,
                    flavor_selectors=_selectors(project),
                    source_intelligence_provider="none",
                    convert=True,
                )
                implementation = (
                    project / "components/legacy-project-wrapper/implementation"
                )
                makefile = implementation / "Makefile"
                makefile.write_text(
                    makefile.read_text().replace("Ran 2 tests", summary)
                )
                candidate = root / "candidate.json"
                status, result = _invoke(
                    "project",
                    "test-receipt",
                    "run-retained",
                    str(candidate),
                    "--project",
                    str(project),
                )
                self.assertEqual(status == 0, accepted, result)
                self.assertEqual(candidate.exists(), accepted)
