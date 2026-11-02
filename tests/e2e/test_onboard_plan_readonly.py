"""A reviewed onboard plan cannot mutate the target it describes."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest import mock

from literate_ai.perf import read_performance_spans
from tests.support.fixtures_test_project_cli import invoke


class OnboardReadOnlyPlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "legacy"
        self.root.mkdir()
        (self.root / "app.py").write_text("print('hello')\n", encoding="utf-8")
        (self.root / "Makefile").write_text("build:\n\t@true\n", encoding="utf-8")

    def snapshot(self):
        return {
            path.relative_to(self.root).as_posix(): (
                path.stat().st_mode,
                path.read_bytes() if path.is_file() else None,
            )
            for path in self.root.rglob("*")
        }

    def plan(self):
        status, envelope = invoke(
            "onboard", "adopt", ".", "--source-intelligence-provider", "none"
        )
        self.assertEqual(status, 0, envelope)
        self.assertFalse(envelope["result"]["writes"])
        return envelope["result"]

    @unittest.skipUnless(os.name != "nt" and shutil.which("make"), "POSIX Make fixture")
    def test_plan_from_target_is_read_only_and_exact_reviewed_identity_applies(self):
        with chdir(self.root), mock.patch.dict(os.environ, {"OBJ_DIR": ""}):
            before = self.snapshot()
            first = self.plan()
            self.assertEqual(self.snapshot(), before)
            second = self.plan()
            self.assertEqual(first["plan_identity"], second["plan_identity"])
            self.assertEqual(self.snapshot(), before)
            status, applied = invoke(
                "onboard",
                "adopt",
                ".",
                "--source-intelligence-provider",
                "none",
                "--apply",
                "--acknowledge",
                "--expect-plan",
                first["plan_identity"],
            )
            self.assertEqual(status, 0, applied)
            retained = (
                self.root / "components/legacy-project-wrapper/implementation/app.py"
            )
            self.assertEqual(retained.read_bytes(), before["app.py"][1])
            # Applying remains mutagenic and retains ordinary timing diagnostics.
            self.assertTrue(read_performance_spans(self.root / "_build"))

    def test_explicit_object_root_does_not_make_read_only_plan_write(self):
        with chdir(self.root):
            for location in ("custom-output", str(self.root / "absolute-output")):
                with (
                    self.subTest(location=Path(location).name),
                    mock.patch.dict(os.environ, {"OBJ_DIR": location}),
                ):
                    before = self.snapshot()
                    first = self.plan()
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(
                        self.plan()["plan_identity"], first["plan_identity"]
                    )

    def test_actual_source_drift_still_refuses_reviewed_plan(self):
        with chdir(self.root), mock.patch.dict(os.environ, {"OBJ_DIR": ""}):
            plan = self.plan()
            (self.root / "new_source.py").write_text("print('new')\n", encoding="utf-8")
            before = self.snapshot()
            status, result = invoke(
                "onboard",
                "adopt",
                ".",
                "--source-intelligence-provider",
                "none",
                "--apply",
                "--acknowledge",
                "--expect-plan",
                plan["plan_identity"],
            )
            self.assertEqual(status, 2, result)
            self.assertEqual(result["error"]["code"], "operator.onboard_plan_changed")
            self.assertEqual((self.root / "app.py").read_bytes(), before["app.py"][1])
            self.assertFalse((self.root / "literate.project.json").exists())

    def test_preexisting_build_content_remains_in_reviewed_quarantine(self):
        build = self.root / "_build"
        build.mkdir()
        (build / "valuable-object.txt").write_bytes(b"retained operator content")
        with chdir(self.root), mock.patch.dict(os.environ, {"OBJ_DIR": ""}):
            before = self.snapshot()
            plan = self.plan()
            self.assertIn("_build", plan["conversion"]["quarantine"]["entries"])
            self.assertEqual(self.snapshot(), before)

    def test_create_plan_leaves_target_and_ambient_build_directory_absent(self):
        parent = self.root.parent
        target = parent / "new-project"
        with chdir(parent), mock.patch.dict(os.environ, {"OBJ_DIR": ""}):
            status, envelope = invoke(
                "onboard",
                "create",
                str(target),
                "--flavor",
                "python",
                "--flavor",
                "macos",
                "--flavor",
                "bazel",
                "--source-intelligence-provider",
                "none",
            )
            self.assertEqual(status, 0, envelope)
            self.assertFalse(envelope["result"]["writes"])
            self.assertFalse(target.exists())
            self.assertFalse((parent / "_build").exists())


if __name__ == "__main__":
    unittest.main()
