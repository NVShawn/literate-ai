"""Conversion preserves Git custody of ignored tracked source and aliases."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters import harness_inventory
from literate_ai.adapters import project_initialization as initialization
from literate_ai.adapters.harness_tree import (
    capture_retained_source_scope,
    copy_retained_source_tree,
)
from tests.support.fixtures_test_project_cli import invoke
from tests.support.fixtures_test_retained_harness_receipts import _adapter, _selectors
from tests.support.symlinks import skip_unless_symlinks_followable


class TrackedIgnoredConversionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / "project"
        (self.root / "src/workspace").mkdir(parents=True)
        (self.root / "src/workspace/tests.py").write_text("VALUE = 7\n")
        (self.root / ".gitignore").write_text("workspace/\n__pycache__/\npackage.zip\n")
        (self.root / "test_main.py").write_text(
            "import unittest\nfrom src.workspace.tests import VALUE\n"
            "class SourceTest(unittest.TestCase):\n"
            "    def test_value(self): self.assertEqual(VALUE, 7)\n"
        )
        python = shlex.quote(sys.executable)
        (self.root / "Makefile").write_text(
            f"build:\n\t{python} -m py_compile test_main.py\n"
            f"test:\n\t{python} -m unittest -q test_main\n"
            f"package:\n\t{python} -c 'import zipfile; "
            'z=zipfile.ZipFile("package.zip", "w"); '
            'z.write("src/workspace/tests.py"); z.close()'
            "'\n"
        )
        self.git("init", "--quiet")
        self.git("add", ".gitignore", "Makefile", "test_main.py")
        self.git("add", "-f", "src/workspace/tests.py")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "baseline",
        )
        (self.root / "src/untracked.tmp").write_bytes(b"untracked operator content")

    def git(self, *args):
        return subprocess.run(
            ("git", "-C", str(self.root), *args),
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout

    def snapshot(self):
        return {
            path.relative_to(self.root).as_posix(): (
                os.readlink(path)
                if path.is_symlink()
                else path.read_bytes()
                if path.is_file()
                else None
            )
            for path in self.root.rglob("*")
            if ".git" not in path.relative_to(self.root).parts
        }

    def test_partial_directory_preserves_index_scope_and_baseline_copy(self):
        quarantine = initialization._quarantine_existing_tree(self.root)
        legacy = self.root / quarantine["directory"]
        scope = capture_retained_source_scope(legacy)
        self.assertIn("src/workspace/tests.py", scope["paths"])
        sandbox = self.parent / "baseline"
        copy_retained_source_tree(legacy, sandbox, scope)
        self.assertEqual(
            (sandbox / "src/workspace/tests.py").read_bytes(), b"VALUE = 7\n"
        )
        self.assertEqual(
            (legacy / "src/untracked.tmp").read_bytes(), b"untracked operator content"
        )
        self.assertIn(
            (quarantine["directory"] + "/src/workspace/tests.py").encode(),
            self.git("ls-files", "-z"),
        )

    def test_failed_tracked_move_restores_only_completed_moves(self):
        before = self.snapshot()
        index = self.git("ls-files", "--stage", "-z")
        run = subprocess.run

        def fail_src(argv, **kwargs):
            if argv[0] == "git" and "mv" in argv and argv[-2] == "src":
                return subprocess.CompletedProcess(
                    argv, 1, b"", b"fixture move failure"
                )
            return run(argv, **kwargs)

        with mock.patch.object(initialization.subprocess, "run", side_effect=fail_src):
            with self.assertRaises(initialization.ProjectInitializationError) as raised:
                initialization._quarantine_existing_tree(self.root)
        self.assertEqual(raised.exception.code, "project.convert_quarantine_failed")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git("ls-files", "--stage", "-z"), index)

    def test_relative_alias_and_staged_working_difference_survive_rollback(self):
        skip_unless_symlinks_followable(self, directory=self.parent)
        alias = self.root / "z-link"
        alias.symlink_to("src/workspace/tests.py")
        self.git("add", "z-link")
        (self.root / "src/workspace/tests.py").write_text("VALUE = 8\n")
        before = self.snapshot()
        index = self.git("ls-files", "--stage", "-z")
        quarantine = initialization._quarantine_existing_tree(self.root)
        legacy = self.root / quarantine["directory"]
        self.assertIn("z-link", capture_retained_source_scope(legacy)["paths"])
        initialization._rollback_converted_tree(self.root, quarantine)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git("ls-files", "--stage", "-z"), index)

    def test_failed_index_write_after_physical_move_is_recovered(self):
        before = self.snapshot()
        index = self.git("ls-files", "--stage", "-z")
        run = subprocess.run

        def physical_only(argv, **kwargs):
            if argv[0] == "git" and "mv" in argv and argv[-2] == "src":
                (self.root / "src").rename(self.root / argv[-1])
                return subprocess.CompletedProcess(argv, 1, b"", b"index write failed")
            return run(argv, **kwargs)

        with mock.patch.object(
            initialization.subprocess, "run", side_effect=physical_only
        ):
            with self.assertRaises(initialization.ProjectInitializationError) as raised:
                initialization._quarantine_existing_tree(self.root)
        self.assertEqual(raised.exception.code, "project.convert_quarantine_failed")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git("ls-files", "--stage", "-z"), index)

    def test_literal_index_paths_survive_quarantine_and_rollback(self):
        for name in ("-tracked", "bracket[1]"):
            (self.root / name).write_bytes(b"literal filename")
            self.git("--literal-pathspecs", "add", "--", name)
        before = self.snapshot()
        index = self.git("ls-files", "--stage", "-z")
        quarantine = initialization._quarantine_existing_tree(self.root)
        initialization._rollback_converted_tree(self.root, quarantine)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git("ls-files", "--stage", "-z"), index)

    def test_escaping_alias_preflight_leaves_all_original_entries_untouched(self):
        skip_unless_symlinks_followable(self, directory=self.parent)
        outside = self.parent / "outside"
        outside.write_bytes(b"outside target")
        (self.root / "z-link").symlink_to(outside)
        before = self.snapshot()
        index = self.git("ls-files", "--stage", "-z")
        with self.assertRaises(initialization.ProjectInitializationError) as raised:
            initialization._quarantine_existing_tree(self.root)
        self.assertEqual(raised.exception.code, "project.convert_unsafe_entry")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git("ls-files", "--stage", "-z"), index)

    def test_failed_git_rollback_keeps_original_and_index_in_quarantine(self):
        quarantine = initialization._quarantine_existing_tree(self.root)
        index = self.git("ls-files", "--stage", "-z")
        run = subprocess.run

        def refuse_restore(argv, **kwargs):
            if argv[0] == "git" and "mv" in argv:
                return subprocess.CompletedProcess(argv, 1, b"", b"fixture refusal")
            return run(argv, **kwargs)

        with mock.patch.object(
            initialization.subprocess, "run", side_effect=refuse_restore
        ):
            with self.assertRaises(initialization.ProjectInitializationError) as raised:
                initialization._rollback_converted_tree(self.root, quarantine)
        self.assertEqual(raised.exception.code, "project.convert_rollback_failed")
        legacy = self.root / quarantine["directory"]
        self.assertEqual(
            (legacy / "src/workspace/tests.py").read_bytes(), b"VALUE = 7\n"
        )
        self.assertEqual(self.git("ls-files", "--stage", "-z"), index)

    def test_recovery_collision_preserves_both_original_and_new_entry(self):
        original = (self.root / ".gitignore").read_bytes()
        run = subprocess.run

        def collide(argv, **kwargs):
            if argv[0] == "git" and "mv" in argv and argv[-2] == "src":
                (self.root / ".gitignore").write_bytes(b"new concurrent entry")
                return subprocess.CompletedProcess(argv, 1, b"", b"fixture refusal")
            return run(argv, **kwargs)

        with mock.patch.object(initialization.subprocess, "run", side_effect=collide):
            with self.assertRaises(initialization.ProjectInitializationError) as raised:
                initialization._quarantine_existing_tree(self.root)
        self.assertEqual(
            raised.exception.code, "project.convert_quarantine_rollback_failed"
        )
        self.assertEqual(
            (self.root / ".gitignore").read_bytes(), b"new concurrent entry"
        )
        retained = list((self.root / "_legacy").glob("*/.gitignore"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), original)
        self.assertEqual(
            (self.root / "src/workspace/tests.py").read_bytes(), b"VALUE = 7\n"
        )

    @unittest.skipUnless(os.name != "nt" and shutil.which("make"), "POSIX Make fixture")
    def test_failed_public_adoption_restores_common_agent_alias_and_index(self):
        skip_unless_symlinks_followable(self, directory=self.parent)
        (self.root / "AGENTS.md").write_bytes(b"# Legacy agent instructions\n")
        (self.root / "CLAUDE.md").symlink_to("AGENTS.md")
        self.git("add", "AGENTS.md", "CLAUDE.md")
        path = self.root / "test_main.py"
        path.write_text(path.read_text().replace("VALUE, 7", "VALUE, 8"))
        before = self.snapshot()
        index = self.git("ls-files", "--stage", "-z")
        status, result = invoke(
            "onboard",
            "adopt",
            str(self.root),
            "--source-intelligence-provider",
            "none",
            "--apply",
            "--acknowledge",
            "--run-baseline",
        )
        self.assertEqual(status, 2, result)
        self.assertEqual(result["error"]["code"], "project.convert_legacy_gate_failed")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git("ls-files", "--stage", "-z"), index)

    @unittest.skipUnless(os.name != "nt" and shutil.which("make"), "POSIX Make fixture")
    def test_phase12_failure_restores_original_tree_and_staged_aliases(self):
        skip_unless_symlinks_followable(self, directory=self.parent)
        # This order makes the alias dangling during reverse lift-shift moves.
        (self.root / "Z-source").write_bytes(b"retained alias target")
        (self.root / "A-link").symlink_to("Z-source")
        self.git("add", "Z-source", "A-link")
        before = self.snapshot()
        index = self.git("ls-files", "--stage", "-z")
        phases = []
        parity = harness_inventory.execute_harness_wrapper_parity

        def reject_phase12(*args, **kwargs):
            phase12 = kwargs["legacy_root"].name == "implementation"
            phases.append(phase12)
            result = parity(*args, **kwargs)
            if phase12:
                raise harness_inventory.HarnessBaselineError(
                    "project.convert_wrapper_parity_failed",
                    "injected Phase 1.2 refusal",
                    {
                        "schema": harness_inventory.HARNESS_PARITY_SCHEMA,
                        "state": "failed",
                        "phases": [],
                    },
                )
            return result

        with mock.patch.object(
            harness_inventory,
            "execute_harness_wrapper_parity",
            side_effect=reject_phase12,
        ):
            status, result = invoke(
                "onboard",
                "adopt",
                str(self.root),
                "--source-intelligence-provider",
                "none",
                "--apply",
                "--acknowledge",
                "--run-baseline",
            )
        self.assertEqual(status, 2, result)
        self.assertEqual(
            result["error"]["code"], "project.convert_wrapper_parity_failed"
        )
        self.assertIn("rolled back", result["error"]["message"])
        self.assertEqual(phases, [False, True])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git("ls-files", "--stage", "-z"), index)

    @unittest.skipUnless(os.name != "nt" and shutil.which("make"), "POSIX Make fixture")
    def test_incomplete_phase12_recovery_retains_source_and_reports_failure(self):
        parity = harness_inventory.execute_harness_wrapper_parity
        run = subprocess.run

        def reject_phase12(*args, **kwargs):
            result = parity(*args, **kwargs)
            if kwargs["legacy_root"].name == "implementation":
                raise harness_inventory.HarnessBaselineError(
                    "project.convert_wrapper_parity_failed",
                    "injected Phase 1.2 refusal",
                    {
                        "schema": harness_inventory.HARNESS_PARITY_SCHEMA,
                        "state": "failed",
                        "phases": [],
                    },
                )
            return result

        def refuse_recovery(argv, **kwargs):
            if (
                argv[0] == "git"
                and "mv" in argv
                and str(argv[-2]).startswith(
                    "components/legacy-project-wrapper/implementation/"
                )
                and str(argv[-1]).startswith("_legacy/")
            ):
                return subprocess.CompletedProcess(
                    argv, 1, b"", b"injected recovery refusal"
                )
            return run(argv, **kwargs)

        with (
            mock.patch.object(
                harness_inventory,
                "execute_harness_wrapper_parity",
                side_effect=reject_phase12,
            ),
            mock.patch.object(
                harness_inventory.subprocess, "run", side_effect=refuse_recovery
            ),
        ):
            status, result = invoke(
                "onboard",
                "adopt",
                str(self.root),
                "--source-intelligence-provider",
                "none",
                "--apply",
                "--acknowledge",
                "--run-baseline",
            )
        self.assertEqual(status, 2, result)
        self.assertEqual(result["error"]["code"], "project.convert_rollback_failed")
        retained = (
            self.root
            / "components/legacy-project-wrapper/implementation/src/workspace/tests.py"
        )
        self.assertEqual(retained.read_bytes(), b"VALUE = 7\n")
        self.assertIn(
            str(retained.relative_to(self.root)).encode(), self.git("ls-files", "-z")
        )

    @unittest.skipUnless(os.name != "nt" and shutil.which("make"), "POSIX Make fixture")
    def test_unexplained_recovery_content_is_preserved(self):
        parity = harness_inventory.execute_harness_wrapper_parity

        def reject_with_extra(*args, **kwargs):
            result = parity(*args, **kwargs)
            if kwargs["legacy_root"].name == "implementation":
                (kwargs["legacy_root"] / "recovery-note").write_bytes(
                    b"keep this operator data"
                )
                raise harness_inventory.HarnessBaselineError(
                    "project.convert_wrapper_parity_failed",
                    "injected Phase 1.2 refusal",
                    {
                        "schema": harness_inventory.HARNESS_PARITY_SCHEMA,
                        "state": "failed",
                        "phases": [],
                    },
                )
            return result

        with mock.patch.object(
            harness_inventory,
            "execute_harness_wrapper_parity",
            side_effect=reject_with_extra,
        ):
            status, result = invoke(
                "onboard",
                "adopt",
                str(self.root),
                "--source-intelligence-provider",
                "none",
                "--apply",
                "--acknowledge",
                "--run-baseline",
            )
        self.assertEqual(status, 2, result)
        self.assertEqual(result["error"]["code"], "project.convert_rollback_failed")
        note = (
            self.root / "components/legacy-project-wrapper/implementation/recovery-note"
        )
        self.assertEqual(note.read_bytes(), b"keep this operator data")
        self.assertTrue(list((self.root / "_legacy").glob("*/src/workspace/tests.py")))

    @unittest.skipUnless(os.name != "nt" and shutil.which("make"), "POSIX Make fixture")
    def test_completed_lift_shift_can_restore_original_git_custody(self):
        before = self.snapshot()
        index = self.git("ls-files", "--stage", "-z")
        result = _adapter().initialize(
            self.root,
            flavor_selectors=_selectors(self.root),
            source_intelligence_provider="none",
            convert=True,
            run_baseline=True,
        )
        quarantine = result["convert_quarantine"]
        lift_shift = json.loads(
            (self.root / ".literate/legacy-lift-shift.json").read_text()
        )
        initialization._restore_lift_shift_quarantine(self.root, quarantine, lift_shift)
        initialization._rollback_converted_tree(self.root, quarantine)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git("ls-files", "--stage", "-z"), index)

    def test_incomplete_quarantine_refuses_before_any_scaffold_deletion(self):
        quarantine = initialization._quarantine_existing_tree(self.root)
        legacy = self.root / quarantine["directory"]
        scaffold = self.root / "scaffold-marker"
        scaffold.write_bytes(b"preserve recovery state")
        (legacy / "unexpected").write_bytes(b"operator recovery note")
        with self.assertRaises(initialization.ProjectInitializationError):
            initialization._rollback_converted_tree(self.root, quarantine)
        self.assertEqual(scaffold.read_bytes(), b"preserve recovery state")
        self.assertEqual(
            (legacy / "unexpected").read_bytes(), b"operator recovery note"
        )
        self.assertEqual(
            (legacy / "src/workspace/tests.py").read_bytes(), b"VALUE = 7\n"
        )

    @unittest.skipUnless(os.name != "nt" and shutil.which("make"), "POSIX Make fixture")
    def test_public_adoption_executes_test_importing_ignored_tracked_module(self):
        status, result = invoke(
            "onboard",
            "adopt",
            str(self.root),
            "--source-intelligence-provider",
            "none",
            "--apply",
            "--acknowledge",
            "--run-baseline",
        )
        self.assertEqual(status, 0, result)
        retained = self.root / "components/legacy-project-wrapper/implementation"
        self.assertEqual(
            (retained / "src/workspace/tests.py").read_bytes(), b"VALUE = 7\n"
        )
        self.assertEqual(
            (retained / "src/untracked.tmp").read_bytes(), b"untracked operator content"
        )
        self.assertIn(
            b"components/legacy-project-wrapper/implementation/src/workspace/tests.py",
            self.git("ls-files", "-z"),
        )


if __name__ == "__main__":
    unittest.main()
