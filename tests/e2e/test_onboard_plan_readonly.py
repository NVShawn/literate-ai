"""A reviewed onboard plan cannot mutate the target it describes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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

    @unittest.skipUnless(
        shutil.which("cargo") and shutil.which("make"),
        "native Cargo and Make are not installed",
    )
    def test_single_cargo_root_uses_reviewed_baseline_commands(self):
        root = self.root.parent / "cargo-workspace"
        root.mkdir()
        (root / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["healthy", "unavailable"]\nresolver = "2"\n'
        )
        for name in ("healthy", "unavailable"):
            member = root / name
            (member / "src").mkdir(parents=True)
            (member / "Cargo.toml").write_text(
                f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n'
            )
            assertion = (
                "assert_eq!(2 + 2, 4);"
                if name == "healthy"
                else 'panic!("this upstream test is not in the selected baseline");'
            )
            (member / "src/lib.rs").write_text(
                "#[cfg(test)] mod tests { #[test] fn actual_case() { "
                + assertion
                + " } }\n"
            )
        broad = subprocess.run(
            ["cargo", "test", "--offline", "--workspace", "-j", "1"],
            cwd=root,
            env={
                **os.environ,
                "CARGO_TARGET_DIR": str(root.parent / "cargo-artifacts"),
            },
            capture_output=True,
            timeout=120,
        )
        self.assertNotEqual(broad.returncode, 0)
        self.assertIn(
            b"this upstream test is not in the selected baseline",
            broad.stdout + broad.stderr,
        )
        original = {
            path.relative_to(root): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }
        selection_path = root.parent / "cargo-selection.json"
        commands = [
            {
                "id": phase,
                "command": command,
                "cwd": ".",
                "evidence": "Cargo.toml",
            }
            for phase, command in (
                ("build", "cargo build --locked --offline -j 1"),
                ("test", "cargo test --locked --offline -p healthy -j 1"),
                ("package", "cargo package --list --offline --allow-dirty"),
            )
        ]
        selection = {
            "schema": "literate-ai/monorepo-adoption-selection@1",
            "components": [{"name": "workspace", "root": ".", "commands": commands}],
            "shared_sources": [],
        }
        arguments = (
            "onboard",
            "adopt",
            str(root),
            "--root-plan",
            str(selection_path),
            "--run-baseline",
            "--allow-unready",
            "--default-branch",
            "main",
            "--source-intelligence-provider",
            "none",
        )
        selection_path.write_text(json.dumps({**selection, "components": []}))
        status, refused = invoke(*arguments)
        self.assertNotEqual(status, 0, refused)
        self.assertEqual(refused["error"]["code"], "monorepo.selection_invalid")
        status, detected_envelope = invoke(*arguments[:3], *arguments[5:])
        self.assertEqual(status, 0, detected_envelope)
        detected_stages = detected_envelope["result"]["conversion"][
            "proposed_wrapper_stages"
        ]
        test_only = json.loads(json.dumps(selection))
        test_only["components"][0]["commands"] = [commands[1]]
        selection_path.write_text(json.dumps(test_only))
        status, scoped_envelope = invoke(*arguments)
        self.assertEqual(status, 0, scoped_envelope)
        scoped_stages = scoped_envelope["result"]["conversion"][
            "proposed_wrapper_stages"
        ]
        self.assertEqual(
            [
                stage
                for stage in scoped_stages
                if stage["id"].split(".", 1)[0] != "test"
            ],
            [
                stage
                for stage in detected_stages
                if stage["id"].split(".", 1)[0] != "test"
            ],
        )
        self.assertTrue(
            {"build", "package"}.issubset(
                {stage["id"].split(".", 1)[0] for stage in detected_stages}
            )
        )
        self.assertEqual(
            [
                stage["command"]
                for stage in scoped_stages
                if stage["id"].split(".", 1)[0] == "test"
            ],
            [commands[1]["command"]],
        )
        selection_path.write_text(json.dumps(selection))
        status, envelope = invoke(*arguments)
        self.assertEqual(status, 0, envelope)
        plan = envelope["result"]
        self.assertTrue(plan["apply_supported"], plan)
        selected_phases = {command["id"] for command in commands}
        planned_stages = plan["conversion"]["proposed_wrapper_stages"]
        self.assertEqual(
            [
                stage["command"]
                for stage in planned_stages
                if stage["id"].split(".", 1)[0] in selected_phases
            ],
            [command["command"] for command in commands],
        )
        self.assertEqual(
            [
                stage
                for stage in planned_stages
                if stage["id"].split(".", 1)[0] not in selected_phases
            ],
            [
                stage
                for stage in detected_stages
                if stage["id"].split(".", 1)[0] not in selected_phases
            ],
        )
        self.assertEqual(
            {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            },
            original,
        )
        selected_bytes = selection_path.read_bytes()
        changed_selection = json.loads(selected_bytes)
        changed_selection["components"][0]["commands"][1]["command"] += " -- --ignored"
        selection_path.write_text(json.dumps(changed_selection))
        status, refused = invoke(
            *arguments,
            "--apply",
            "--acknowledge",
            "--expect-plan",
            plan["plan_identity"],
        )
        self.assertNotEqual(status, 0, refused)
        self.assertEqual(refused["error"]["code"], "operator.onboard_plan_changed")
        selection_path.write_bytes(selected_bytes)
        from literate_ai.adapters import project_initialization as adoption

        quarantine = adoption._quarantine_existing_tree
        changed_path = Path("healthy/src/lib.rs")
        concurrent_bytes = original[changed_path] + b"\n// concurrent source edit\n"
        race_root = root.parent / "cargo-concurrent-change"
        shutil.copytree(root, race_root)
        race_arguments = (*arguments[:2], str(race_root), *arguments[3:])
        status, race_envelope = invoke(*race_arguments)
        self.assertEqual(status, 0, race_envelope)
        race_plan = race_envelope["result"]
        retained_quarantines = []
        foreign_bytes = b"concurrent root file must survive refusal\n"

        def change_after_quarantine(target):
            moved = quarantine(target)
            self.assertIsNotNone(moved)
            retained = target / moved["directory"]
            retained_quarantines.append(retained)
            (retained / changed_path).write_bytes(concurrent_bytes)
            (target / "new-concurrent.txt").write_bytes(foreign_bytes)
            return moved

        with mock.patch.object(
            adoption, "_quarantine_existing_tree", side_effect=change_after_quarantine
        ):
            status, refused = invoke(
                *race_arguments,
                "--apply",
                "--acknowledge",
                "--expect-plan",
                race_plan["plan_identity"],
            )
        self.assertNotEqual(status, 0, refused)
        self.assertEqual(refused["error"]["code"], "monorepo.source_changed")
        self.assertIn("recovery incomplete", json.dumps(refused))
        self.assertEqual(len(retained_quarantines), 1)
        retained = retained_quarantines[0]
        self.assertEqual((retained / changed_path).read_bytes(), concurrent_bytes)
        self.assertEqual((race_root / "new-concurrent.txt").read_bytes(), foreign_bytes)
        self.assertFalse((race_root / "literate.project.json").exists())
        for relative, content in original.items():
            if relative != changed_path:
                self.assertEqual((retained / relative).read_bytes(), content)
            self.assertEqual((root / relative).read_bytes(), content)
        # The refused transaction remains intact; the separate unchanged source
        # earns its own current plan and completes the real selected baseline.
        status, envelope = invoke(*arguments)
        self.assertEqual(status, 0, envelope)
        plan = envelope["result"]
        status, applied = invoke(
            *arguments,
            "--apply",
            "--acknowledge",
            "--expect-plan",
            plan["plan_identity"],
        )
        self.assertEqual(status, 0, applied)
        self.assertTrue(applied["ok"], applied)
        self.assertTrue((root / "literate.project.json").is_file())
        baseline = json.loads(
            (root / ".literate/legacy-harness-baseline.json").read_text()
        )
        self.assertEqual(baseline["state"], "passed")
        lift_shift = json.loads((root / ".literate/legacy-lift-shift.json").read_text())
        self.assertEqual(lift_shift["state"], "passed")
        implementation = root / lift_shift["implementation_directory"]
        for relative, content in original.items():
            self.assertEqual((implementation / relative).read_bytes(), content)
        self.assertEqual(
            [phase["command"] for phase in baseline["phases"]],
            [command["command"] for command in commands],
        )
        self.assertTrue(all(phase["exit_code"] == 0 for phase in baseline["phases"]))

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
