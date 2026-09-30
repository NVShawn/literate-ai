"""Explicit multi-root plans never infer custody or call the flat converter."""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters import monorepo_adoption
from literate_ai.adapters.harness_inventory import inspect_harness
from literate_ai.adapters.monorepo_adoption import (
    SELECTION_SCHEMA,
    MonorepoAdoptionError,
    build_root_candidates,
    plan_monorepo_adoption,
)
from literate_ai.adapters.project_initialization import plan_convert
from literate_ai.cli import main
from literate_ai.contracts import canonical_identity


class MonorepoAdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="litai roots ")
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name).resolve()
        self.root = base / "project"
        self.root.mkdir()
        self.selection_file = base / "selection.json"
        for name in ("kit", "runtime"):
            (self.root / name).mkdir()
            (self.root / name / "Makefile").write_text(
                "all:\n\t@echo build\ntest:\n\t@echo test\n", encoding="utf-8"
            )
            (self.root / name / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.root / "shared").mkdir()
        (self.root / "shared" / "api.py").write_text("API = 1\n", encoding="utf-8")
        (self.root / "repo.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.selection = {
            "schema": SELECTION_SCHEMA,
            "components": [
                {
                    "name": name,
                    "root": name,
                    "commands": [
                        {
                            "id": phase,
                            "command": command,
                            "cwd": name,
                            "evidence": f"{name}/Makefile",
                        }
                        for phase, command in (("build", "make"), ("test", "make test"))
                    ],
                }
                for name in ("kit", "runtime")
            ],
            "shared_sources": [
                {"path": "shared", "owner": "kit", "consumers": ["runtime"]},
                {"path": "repo.sh", "owner": "kit", "consumers": ["runtime"]},
            ],
        }

    def plan(self, selection=None):
        self.selection_file.write_text(
            json.dumps(self.selection if selection is None else selection),
            encoding="utf-8",
        )
        return plan_monorepo_adoption(
            self.root, self.selection_file, inspect_harness(self.root), submodules=[]
        )

    def snapshot(self):
        return {
            p.relative_to(self.root).as_posix(): p.read_bytes() if p.is_file() else None
            for p in self.root.rglob("*")
        }

    def refuse(self, suffix, selection=None):
        before = self.snapshot()
        with self.assertRaises(MonorepoAdoptionError) as raised:
            self.plan(selection)
        self.assertEqual(raised.exception.code, f"monorepo.{suffix}")
        self.assertEqual(self.snapshot(), before)

    def test_candidates_preserve_independent_markers_without_selecting_components(self):
        candidates = build_root_candidates(inspect_harness(self.root))
        self.assertEqual([c["root"] for c in candidates], [".", "kit", "runtime"])
        self.assertTrue(
            all(
                c["authority"] == "candidate-only" and not c["selected"]
                for c in candidates
            )
        )
        self.assertEqual(candidates[1]["markers"], ["kit/Makefile"])
        self.assertEqual(candidates[1]["stages"], [])  # No invented command.

    def test_issue_365_repo_toml_and_bazel_roots_remain_distinct_candidates(self):
        for name in ("kit", "rendering", "runtime"):
            (self.root / name).mkdir(exist_ok=True)
            (self.root / name / "repo.toml").write_text(
                "[repo_build]\n", encoding="utf-8"
            )
        (self.root / "kit" / "MODULE.bazel").write_text("", encoding="utf-8")
        conversion = plan_convert(self.root, default_branch="main")
        self.assertEqual(
            [c["root"] for c in conversion["build_root_candidates"]],
            [".", "kit", "rendering", "runtime"],
        )
        self.assertIsNone(conversion["root_refinement"])
        self.assertEqual(conversion["landing_stage"], "wrapped")

    def test_exact_plan_has_exclusive_ownership_and_explicit_shared_consumers(self):
        before = self.snapshot()
        result = self.plan()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(result["writes"])
        self.assertFalse(result["execution_performed"])
        self.assertTrue(result["apply_supported"])
        self.assertEqual(result["blockers"], [])
        kit, runtime = result["components"]
        self.assertEqual(kit["commands"][0]["cwd"], "kit")
        self.assertEqual(runtime["commands"][0]["cwd"], "runtime")
        self.assertEqual(runtime["shared_inputs"], ["repo.sh", "shared/api.py"])
        self.assertFalse(set(kit["owned_sources"]) & set(runtime["owned_sources"]))
        self.assertEqual(
            set(kit["owned_sources"] + runtime["owned_sources"]),
            set(before) - {"kit", "runtime", "shared"},
        )
        identity = result.pop("plan_identity")
        self.assertEqual(identity, canonical_identity(result).uri)

    def test_source_bytes_commands_and_executable_bits_change_identity(self):
        original = self.plan()
        (self.root / "kit" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        changed = self.plan()
        self.assertNotEqual(original["source_identity"], changed["source_identity"])
        self.selection["components"][0]["commands"][0]["command"] = "make all"
        command_changed = self.plan()
        self.assertNotEqual(changed["plan_identity"], command_changed["plan_identity"])
        self.assertEqual(changed["source_identity"], command_changed["source_identity"])
        if os.name != "nt":
            (self.root / "kit" / "app.py").chmod(0o755)
            self.assertNotEqual(
                command_changed["source_identity"], self.plan()["source_identity"]
            )

    def test_rejects_parent_child_roots(self):
        self.selection["components"][0]["root"] = "."
        self.refuse("root_overlap")

    def test_rejects_unknown_roots_and_duplicate_names(self):
        selection = copy.deepcopy(self.selection)
        selection["components"][0]["root"] = "shared"
        self.refuse("root_undetected", selection)
        selection = copy.deepcopy(self.selection)
        selection["components"][1]["name"] = "kit"
        self.refuse("duplicate_component", selection)

    def test_rejects_nonportable_names_and_unsafe_paths(self):
        for name in ("../kit", "CON", "con", "aux", "lpt1", "a" * 49):
            with self.subTest(name=name):
                selection = copy.deepcopy(self.selection)
                selection["components"][0]["name"] = name
                self.refuse("name_invalid", selection)
        for path in ("../kit", "./kit", "kit/", "C:/kit", "kit\\child", "/kit"):
            with self.subTest(path=path):
                selection = copy.deepcopy(self.selection)
                selection["components"][0]["root"] = path
                self.refuse("path_invalid", selection)

    def test_rejects_unowned_added_and_unknown_shared_files(self):
        (self.root / "new.txt").write_text("new", encoding="utf-8")
        self.refuse("ownership_incomplete")
        self.selection["shared_sources"].append(
            {"path": "missing", "owner": "kit", "consumers": []}
        )
        self.refuse("source_unadmitted")

    def test_rejects_shared_overlap_and_invalid_participants(self):
        for item, code in (
            (
                {"path": "shared/api.py", "owner": "kit", "consumers": []},
                "ownership_overlap",
            ),
            ({"path": "kit", "owner": "runtime", "consumers": []}, "ownership_overlap"),
            ({"path": "unused", "owner": "unknown", "consumers": []}, "owner_invalid"),
            (
                {"path": "unused", "owner": "kit", "consumers": ["kit"]},
                "consumer_invalid",
            ),
            (
                {"path": "unused", "owner": "kit", "consumers": ["runtime", "runtime"]},
                "consumer_invalid",
            ),
        ):
            with self.subTest(code=code, item=item):
                selection = copy.deepcopy(self.selection)
                selection["shared_sources"].append(item)
                self.refuse(code, selection)

    def test_rejects_missing_duplicate_and_unowned_commands(self):
        for update, code in (
            ({"commands": []}, "commands_missing"),
            (
                {"commands": [self.selection["components"][0]["commands"][0]] * 2},
                "command_invalid",
            ),
            (
                {
                    "commands": [
                        {
                            "id": "test",
                            "command": "make",
                            "cwd": "runtime",
                            "evidence": "kit/Makefile",
                        }
                    ]
                },
                "command_cwd_invalid",
            ),
            (
                {
                    "commands": [
                        {
                            "id": "test",
                            "command": "make",
                            "cwd": "kit",
                            "evidence": "runtime/Makefile",
                        }
                    ]
                },
                "command_evidence_invalid",
            ),
        ):
            with self.subTest(code=code):
                selection = copy.deepcopy(self.selection)
                selection["components"][0].update(update)
                self.refuse(code, selection)

    def test_shared_aggregate_command_requires_explicit_usage(self):
        self.selection["components"][1]["commands"][0] = {
            "id": "build",
            "command": "./repo.sh build runtime",
            "cwd": ".",
            "evidence": "repo.sh",
        }
        self.assertEqual(self.plan()["components"][1]["commands"][0]["cwd"], ".")
        self.selection["shared_sources"][1]["consumers"] = []
        self.refuse("command_evidence_invalid")

    def test_planning_does_not_execute_declared_commands(self):
        self.selection["components"][0]["commands"][0]["command"] = (
            "touch planning-must-not-execute"
        )
        before = self.snapshot()
        self.plan()
        self.assertEqual(self.snapshot(), before)

    def test_source_race_requires_replanning(self):
        observe = monorepo_adoption._source_members
        calls = 0

        def racing_members(root, paths):
            nonlocal calls
            members = observe(root, paths)
            calls += 1
            if calls == 1:
                (self.root / "kit" / "app.py").write_text("raced\n", encoding="utf-8")
            return members

        with mock.patch.object(
            monorepo_adoption, "_source_members", side_effect=racing_members
        ):
            with self.assertRaises(MonorepoAdoptionError) as raised:
                self.plan()
        self.assertEqual(raised.exception.code, "monorepo.source_changed")

    def test_selection_race_requires_replanning(self):
        observe = monorepo_adoption._source_members

        def racing_members(root, paths):
            selection = copy.deepcopy(self.selection)
            selection["components"][0]["commands"][0]["command"] = "make changed"
            self.selection_file.write_text(json.dumps(selection), encoding="utf-8")
            return observe(root, paths)

        with mock.patch.object(
            monorepo_adoption, "_source_members", side_effect=racing_members
        ):
            with self.assertRaises(MonorepoAdoptionError) as raised:
                self.plan()
        self.assertEqual(raised.exception.code, "monorepo.selection_changed")

    def test_symlinked_source_is_refused_without_following_target(self):
        link = self.root / "kit" / "external.py"
        try:
            link.symlink_to(self.selection_file)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        # Do not use snapshot(), which intentionally reads regular fixture bytes.
        with self.assertRaisesRegex(MonorepoAdoptionError, "indirect custody"):
            self.plan()

    def test_nested_history_and_submodules_are_not_flattened(self):
        (self.root / "kit" / ".git").mkdir()
        self.refuse("separate_history")
        self.selection_file.write_text(json.dumps(self.selection), encoding="utf-8")
        with self.assertRaisesRegex(MonorepoAdoptionError, "submodules"):
            plan_monorepo_adoption(
                self.root,
                self.selection_file,
                inspect_harness(self.root),
                submodules=[{"path": "child"}],
            )

    def test_selection_rejects_unknown_fields_duplicate_json_and_oversize(self):
        selection = {**self.selection, "apply": True}
        self.refuse("selection_invalid", selection)
        for data in ('{"schema": 1, "schema": 2}', " " * (256 * 1024 + 1)):
            self.selection_file.write_text(data, encoding="utf-8")
            with self.assertRaises(MonorepoAdoptionError):
                plan_monorepo_adoption(
                    self.root,
                    self.selection_file,
                    inspect_harness(self.root),
                    submodules=[],
                )

    def test_git_visible_membership_does_not_admit_ignored_build_output(self):
        subprocess.run(
            ["git", "init", "-q", str(self.root)], check=True, capture_output=True
        )
        (self.root / ".gitignore").write_text("cache/\n", encoding="utf-8")
        self.selection["shared_sources"].append(
            {"path": ".gitignore", "owner": "kit", "consumers": []}
        )
        first = self.plan()
        (self.root / "cache").mkdir()
        (self.root / "cache" / "artifact").write_bytes(b"ignored")
        self.assertEqual(first["source_identity"], self.plan()["source_identity"])
        (self.root / "runtime" / "new.py").write_text("new", encoding="utf-8")
        self.assertNotEqual(first["source_identity"], self.plan()["source_identity"])

    def test_cli_refined_plan_requires_explicit_baseline_execution_for_apply(self):
        self.plan()
        before = self.snapshot()
        args = [
            "--json",
            "onboard",
            "adopt",
            str(self.root),
            "--default-branch",
            "main",
            "--root-plan",
            str(self.selection_file),
        ]
        with (
            mock.patch(
                "literate_ai.cli.operator.inspect_host_preflight", return_value={}
            ),
            mock.patch("literate_ai.cli.operator.init_project_from_args") as mutator,
            mock.patch(
                "literate_ai.cli.dispatch._perf_build_root",
                return_value=self.root / "_build",
            ),
        ):
            output, errors = io.StringIO(), io.StringIO()
            self.assertEqual(
                main(args, stdout=output, stderr=errors), 0, errors.getvalue()
            )
            result = json.loads(output.getvalue())["result"]
            self.assertFalse(result["apply_supported"])
            self.assertIn("monorepo.baseline_execution_required", result["blockers"])
            self.assertIsNone(result["landing_stage"])
            self.assertEqual(result["next_commands"], [])
            for flags, code in (
                (["--apply"], "operator.onboard_acknowledgement_required"),
                (
                    ["--apply", "--acknowledge", "--allow-unready"],
                    "operator.onboard_plan_blocked",
                ),
                (
                    [
                        "--apply",
                        "--acknowledge",
                        "--expect-plan",
                        result["plan_identity"],
                    ],
                    "operator.onboard_plan_blocked",
                ),
            ):
                errors = io.StringIO()
                self.assertNotEqual(
                    main(args + flags, stdout=io.StringIO(), stderr=errors), 0
                )
                self.assertEqual(json.loads(errors.getvalue())["error"]["code"], code)
            (self.root / "kit" / "app.py").write_text("changed\n", encoding="utf-8")
            errors = io.StringIO()
            self.assertNotEqual(
                main(
                    args
                    + [
                        "--apply",
                        "--acknowledge",
                        "--expect-plan",
                        result["plan_identity"],
                    ],
                    stdout=io.StringIO(),
                    stderr=errors,
                ),
                0,
            )
            self.assertEqual(
                json.loads(errors.getvalue())["error"]["code"],
                "operator.onboard_plan_changed",
            )
            mutator.assert_not_called()
        after = self.snapshot()
        self.assertEqual(set(before), set(after))
        self.assertEqual(
            {k: v for k, v in before.items() if k != "kit/app.py"},
            {k: v for k, v in after.items() if k != "kit/app.py"},
        )

    def test_cli_refined_apply_routes_the_reviewed_plan_to_the_mutator(self):
        self.plan()
        args = [
            "--json",
            "onboard",
            "adopt",
            str(self.root),
            "--default-branch",
            "main",
            "--root-plan",
            str(self.selection_file),
            "--run-baseline",
        ]
        initialized = {"schema": "literate-ai/project-initialization@6"}
        with (
            mock.patch(
                "literate_ai.cli.operator.inspect_host_preflight", return_value={}
            ),
            mock.patch(
                "literate_ai.cli.operator.init_project_from_args",
                return_value=initialized,
            ) as mutator,
            mock.patch(
                "literate_ai.cli.dispatch._perf_build_root",
                return_value=self.root / "_build",
            ),
        ):
            output, errors = io.StringIO(), io.StringIO()
            self.assertEqual(
                main(args, stdout=output, stderr=errors), 0, errors.getvalue()
            )
            plan = json.loads(output.getvalue())["result"]
            self.assertTrue(plan["apply_supported"])
            self.assertEqual(plan["blockers"], [])
            output, errors = io.StringIO(), io.StringIO()
            self.assertEqual(
                main(
                    args
                    + [
                        "--apply",
                        "--acknowledge",
                        "--expect-plan",
                        plan["plan_identity"],
                    ],
                    stdout=output,
                    stderr=errors,
                ),
                0,
                errors.getvalue(),
            )
            namespace = mutator.call_args.args[0]
            self.assertEqual(namespace.root_plan, str(self.selection_file))
            self.assertTrue(namespace.run_baseline)


if __name__ == "__main__":
    unittest.main()
