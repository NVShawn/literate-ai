"""Real per-root retained execution with shared-input and rollback boundaries."""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters import monorepo_components as components
from literate_ai.adapters.component_markdown import parse_component_markdown
from literate_ai.adapters.harness_inventory import HarnessBaselineError
from literate_ai.adapters.lifecycle_lock import (
    ProjectLifecycleLockError,
    project_lifecycle_lock,
)
from literate_ai.adapters.monorepo_adoption import (
    SELECTION_SCHEMA,
    MonorepoAdoptionError,
)
from literate_ai.adapters.project_initialization import plan_convert
from literate_ai.contracts import canonical_identity, canonical_json_bytes


class MonorepoComponentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="litai component receipts ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        self.bundle = self.base / "bundle"
        self.selection_file = self.base / "selection.json"
        self.selection = {
            "schema": SELECTION_SCHEMA,
            "components": [],
            "shared_sources": [
                {"path": "shared", "owner": "kit", "consumers": ["runtime"]}
            ],
        }
        (self.source / "shared").mkdir()
        (self.source / "shared" / "api.py").write_text("API = 1\n", encoding="utf-8")
        for name in ("kit", "runtime"):
            root = self.source / name
            root.mkdir()
            (root / "Makefile").write_text("all:\n\t@echo build\n", encoding="utf-8")
            (root / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / "test_value.py").write_text(
                "import pathlib\nimport unittest\nfrom value import VALUE\n"
                "class TestValue(unittest.TestCase):\n"
                "    def test_value(self):\n"
                "        self.assertGreater(VALUE, 0)\n"
                "        self.assertTrue(pathlib.Path('../shared/api.py').is_file())\n"
                "        self.assertFalse(pathlib.Path('../"
                + ("runtime" if name == "kit" else "kit")
                + "/value.py').exists())\n",
                encoding="utf-8",
            )
            interpreter = "python" if os.name == "nt" else "python3"
            self.selection["components"].append(
                {
                    "name": name,
                    "root": name,
                    "commands": [
                        {
                            "id": "test",
                            "command": f"{interpreter} -m unittest discover -v",
                            "cwd": name,
                            "evidence": f"{name}/test_value.py",
                        }
                    ],
                }
            )

    def stage(self, destination=None):
        self.selection_file.write_text(json.dumps(self.selection), encoding="utf-8")
        self.plan = plan_convert(
            self.source, default_branch="main", root_plan=self.selection_file
        )["root_refinement"]
        return components.stage_monorepo_components(
            self.source,
            self.selection_file,
            destination or self.bundle,
            expected_plan_identity=self.plan["plan_identity"],
            acknowledged=True,
            default_branch="main",
        )

    def run_component(self, name, output=None):
        output = output or self.base / f"{name}.receipt.json"
        result = components.run_component_retained_harness(
            self.source,
            self.bundle,
            name,
            output,
            acknowledged=True,
            worker_id="local-test",
            timeout_seconds=30,
        )
        return output, result

    def check(self, name, path):
        return components.check_component_retained_receipt(
            self.source, self.bundle, name, path
        )

    def snapshot(self, root):
        return {
            p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*")
            if p.is_file()
        }

    def retained_bundle(self):
        self.manifest = self.stage()
        self.receipts = {
            name: self.run_component(name)[0] for name in ("kit", "runtime")
        }

    def check_bundle(self, receipts=None, identity=None):
        return components.check_monorepo_retained_receipts(
            self.source,
            self.bundle,
            self.receipts if receipts is None else receipts,
            expected_bundle_identity=identity or self.manifest["bundle_identity"],
        )

    def test_whole_bundle_check_requires_every_real_receipt_without_writes(self):
        self.retained_bundle()
        before = self.snapshot(self.base)
        with mock.patch.object(components, "execute_retained_harness") as execute:
            result = self.check_bundle()
            execute.assert_not_called()
        self.assertEqual(self.snapshot(self.base), before)
        self.assertEqual(result["state"], "current")
        self.assertEqual(result["stage"], "staged")
        self.assertEqual(result["authority"], "original-source")
        self.assertFalse(result["project_initialized"])
        self.assertFalse(result["writes"])
        self.assertFalse(result["execution_performed"])
        self.assertEqual(
            [c["component"] for c in result["components"]], ["kit", "runtime"]
        )
        self.assertEqual(
            result["identity"],
            canonical_identity(
                {k: v for k, v in result.items() if k != "identity"}
            ).uri,
        )

    def test_whole_bundle_refuses_missing_extra_and_substituted_receipts(self):
        self.retained_bundle()
        for receipts in (
            {},
            {"kit": self.receipts["kit"]},
            {**self.receipts, "foreign": self.receipts["kit"]},
        ):
            with self.subTest(receipts=list(receipts)):
                with self.assertRaisesRegex(
                    MonorepoAdoptionError, "exactly one receipt"
                ):
                    self.check_bundle(receipts)
        with self.assertRaises(MonorepoAdoptionError):
            self.check_bundle(
                {"kit": self.receipts["kit"], "runtime": self.receipts["kit"]}
            )

    def test_whole_bundle_pins_manifest_and_every_planned_projection(self):
        self.retained_bundle()
        with self.assertRaisesRegex(MonorepoAdoptionError, "bundle identity changed"):
            self.check_bundle(identity="sha256:" + "0" * 64)
        before = self.snapshot(self.bundle)
        for path in (
            "plan.json",
            "components/kit/component.md",
            "components/runtime/binding.json",
            "bundle.json",
        ):
            with self.subTest(path=path):
                (self.bundle / path).write_bytes(b"{}\n")
                with self.assertRaises(MonorepoAdoptionError):
                    self.check_bundle()
                (self.bundle / path).write_bytes(before[path])

    def test_rehashed_manifest_cannot_omit_or_override_planned_projections(self):
        self.retained_bundle()
        manifest_path = self.bundle / "bundle.json"
        original = json.loads(manifest_path.read_bytes())
        for mutate in (
            lambda m: m["components"].pop(),
            lambda m: m["components"].append("kit"),
            lambda m: m["files"].pop("components/runtime/binding.json"),
            lambda m: m["files"].update({"foreign.json": "sha256:" + "0" * 64}),
        ):
            manifest = copy.deepcopy(original)
            mutate(manifest)
            manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
            with self.assertRaises(MonorepoAdoptionError):
                self.check_bundle(identity=canonical_identity(manifest).uri)

    def test_rehashed_binding_cannot_override_its_reviewed_plan(self):
        self.retained_bundle()
        manifest_path = self.bundle / "bundle.json"
        manifest = json.loads(manifest_path.read_bytes())
        path = "components/kit/binding.json"
        binding_path = self.bundle / path
        binding = json.loads(binding_path.read_bytes())
        binding["component"]["commands"][0]["command"] = "echo bypass"
        content = canonical_json_bytes(binding) + b"\n"
        binding_path.write_bytes(content)
        manifest["files"][path] = components._bytes_identity(content)
        manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
        with self.assertRaisesRegex(MonorepoAdoptionError, "planned projections"):
            self.check_bundle(identity=canonical_identity(manifest).uri)

    def test_whole_bundle_refuses_unowned_source_additions(self):
        self.retained_bundle()
        (self.source / "unowned.py").write_text("NEW = 1\n", encoding="utf-8")
        for name, path in self.receipts.items():
            self.assertEqual(self.check(name, path)["state"], "current")
        with self.assertRaisesRegex(MonorepoAdoptionError, "whole-source membership"):
            self.check_bundle()

    def test_whole_bundle_refuses_shared_source_drift(self):
        self.retained_bundle()
        (self.source / "shared" / "api.py").write_text("API = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(
            MonorepoAdoptionError, "current Component revision"
        ):
            self.check_bundle()
        self.receipts["kit"] = self.run_component("kit", self.base / "new-kit.json")[0]
        with self.assertRaisesRegex(
            MonorepoAdoptionError, "current Component revision"
        ):
            self.check_bundle()
        self.receipts["runtime"] = self.run_component(
            "runtime", self.base / "new-runtime.json"
        )[0]
        self.assertEqual(self.check_bundle()["state"], "current")

    def test_whole_bundle_refuses_membership_race_while_hashing(self):
        self.retained_bundle()
        original = components._source_members

        def add_source(*args):
            members = original(*args)
            (self.source / "unowned.py").write_text("NEW = 1\n", encoding="utf-8")
            return members

        with mock.patch.object(components, "_source_members", side_effect=add_source):
            with self.assertRaisesRegex(MonorepoAdoptionError, "membership changed"):
                self.check_bundle()

    def test_whole_bundle_refuses_indirect_plan_or_receipt(self):
        self.retained_bundle()
        for path in (self.bundle / "plan.json", self.receipts["runtime"]):
            with self.subTest(path=path.name):
                content = path.read_bytes()
                external = self.base / "external.json"
                external.write_bytes(content)
                path.unlink()
                try:
                    path.symlink_to(external)
                except OSError as exc:
                    path.write_bytes(content)
                    self.skipTest(f"symlinks unavailable: {exc}")
                with self.assertRaisesRegex(
                    MonorepoAdoptionError, "direct regular files"
                ):
                    self.check_bundle()
                path.unlink()
                path.write_bytes(content)

    def test_whole_bundle_does_not_relabel_one_current_receipt_as_all_current(self):
        self.retained_bundle()
        (self.source / "kit" / "value.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.assertEqual(
            self.check("runtime", self.receipts["runtime"])["state"], "current"
        )
        with self.assertRaisesRegex(
            MonorepoAdoptionError, "current Component revision"
        ):
            self.check_bundle()
        self.receipts["kit"] = self.run_component("kit", self.base / "new-kit.json")[0]
        self.assertEqual(self.check_bundle()["state"], "current")

    def test_whole_bundle_rechecks_previously_checked_source_receipts_and_projection(
        self,
    ):
        self.retained_bundle()
        original = components.check_component_retained_receipt
        paths = [
            self.source / "kit" / "value.py",
            self.receipts["kit"],
            self.bundle / "components" / "kit" / "component.md",
        ]
        for path in paths:
            with self.subTest(path=path.name):
                content = path.read_bytes()

                def mutate_after_check(*args, changed_path=path):
                    result = original(*args)
                    if args[2] == "runtime":
                        changed_path.write_bytes(b"changed after previous check\n")
                    return result

                with mock.patch.object(
                    components,
                    "check_component_retained_receipt",
                    side_effect=mutate_after_check,
                ):
                    with self.assertRaises(MonorepoAdoptionError):
                        self.check_bundle()
                path.write_bytes(content)

    def test_install_reopens_complete_receipts_and_publishes_source_free_components(
        self,
    ):
        self.retained_bundle()
        project = self.base / "project"
        project.mkdir()
        retained = project / "retained"
        shutil.copytree(self.source, retained)
        result = components.install_monorepo_components(
            retained,
            self.bundle,
            project,
            self.receipts,
            expected_bundle_identity=self.manifest["bundle_identity"],
        )
        self.assertEqual(result["state"], "current")
        self.assertEqual(result["stage"], "retained")
        self.assertEqual(result["authority"], "original-source")
        self.assertEqual(result["components"], ["kit", "runtime"])
        self.assertEqual(
            {item["state"] for item in result["boundary_transfer"].values()},
            {"retained-source"},
        )
        self.assertEqual(
            {item["refresh_state"] for item in result["boundary_transfer"].values()},
            {"current"},
        )
        self.assertFalse(result["source_copied"])
        self.assertEqual(
            components.check_installed_monorepo_components(project), result
        )
        for name in result["components"]:
            component = project / "components" / name
            self.assertTrue((component / "component.md").is_file())
            self.assertTrue((component / "binding.json").is_file())
        self.assertEqual(list((project / "components").rglob("*.py")), [])

    def test_install_rolls_back_owned_projection_and_preserves_foreign_paths(self):
        self.retained_bundle()
        project = self.base / "project"
        project.mkdir()
        retained = project / "retained"
        shutil.copytree(self.source, retained)
        foreign = project / "components" / "runtime"
        foreign.mkdir(parents=True)
        (foreign / "foreign.txt").write_text("keep\n", encoding="utf-8")
        with self.assertRaisesRegex(MonorepoAdoptionError, "must be new"):
            components.install_monorepo_components(
                retained,
                self.bundle,
                project,
                self.receipts,
                expected_bundle_identity=self.manifest["bundle_identity"],
            )
        self.assertEqual(
            (foreign / "foreign.txt").read_text(encoding="utf-8"), "keep\n"
        )
        self.assertFalse((project / "components" / "kit").exists())
        self.assertFalse((project / ".literate").exists())

    def test_refresh_reuses_unaffected_receipt_and_rolls_back_partial_replace(self):
        self.retained_bundle()
        project = self.base / "project"
        project.mkdir()
        retained = project / "retained"
        shutil.copytree(self.source, retained)
        components.install_monorepo_components(
            retained,
            self.bundle,
            project,
            self.receipts,
            expected_bundle_identity=self.manifest["bundle_identity"],
        )
        (retained / "kit" / "app.py").write_text(
            "print('kit')\n# refreshed\n", encoding="utf-8"
        )
        metadata_before = self.snapshot(project / ".literate") | {
            f"components/{path.relative_to(project / 'components').as_posix()}": data
            for path, data in (
                (path, path.read_bytes())
                for path in (project / "components").rglob("*")
                if path.is_file()
            )
        }
        original_replace = components.os.replace
        calls = 0

        def fail_once(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected replace failure")
            return original_replace(source, destination)

        with mock.patch.object(components.os, "replace", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "injected replace failure"):
                components.refresh_installed_monorepo_components(
                    project, acknowledged=True
                )
        metadata_after = self.snapshot(project / ".literate") | {
            f"components/{path.relative_to(project / 'components').as_posix()}": data
            for path, data in (
                (path, path.read_bytes())
                for path in (project / "components").rglob("*")
                if path.is_file()
            )
        }
        self.assertEqual(metadata_after, metadata_before)

        refreshed = components.refresh_installed_monorepo_components(
            project, acknowledged=True
        )
        self.assertEqual(refreshed["executed_components"], ["kit"])
        self.assertEqual(refreshed["reused_components"], ["runtime"])

    def test_refresh_rolls_back_when_reopened_custody_validation_fails(self):
        self.retained_bundle()
        project = self.base / "project"
        project.mkdir()
        retained = project / "retained"
        shutil.copytree(self.source, retained)
        components.install_monorepo_components(
            retained,
            self.bundle,
            project,
            self.receipts,
            expected_bundle_identity=self.manifest["bundle_identity"],
        )
        (retained / "kit" / "app.py").write_text(
            "print('kit')\n# refreshed\n", encoding="utf-8"
        )
        before = self.snapshot(project / ".literate") | {
            f"components/{path.relative_to(project / 'components').as_posix()}": data
            for path, data in (
                (path, path.read_bytes())
                for path in (project / "components").rglob("*")
                if path.is_file()
            )
        }
        original_replace = components._replace_payloads

        def fail_reopen(selected, payloads, *, validate=None):
            def reject():
                raise MonorepoAdoptionError("injected", "reopen failure")

            return original_replace(selected, payloads, validate=reject)

        with mock.patch.object(
            components, "_replace_payloads", side_effect=fail_reopen
        ):
            with self.assertRaisesRegex(MonorepoAdoptionError, "did not reopen"):
                components.refresh_installed_monorepo_components(
                    project, acknowledged=True
                )
        after = self.snapshot(project / ".literate") | {
            f"components/{path.relative_to(project / 'components').as_posix()}": data
            for path, data in (
                (path, path.read_bytes())
                for path in (project / "components").rglob("*")
                if path.is_file()
            )
        }
        self.assertEqual(after, before)

    def test_stage_materializes_distinct_source_free_components_without_source_writes(
        self,
    ):
        before = self.snapshot(self.source)
        manifest = self.stage()
        self.assertEqual(manifest["components"], ["kit", "runtime"])
        self.assertFalse(manifest["source_copied"])
        self.assertFalse(manifest["project_initialized"])
        self.assertEqual(self.snapshot(self.source), before)
        self.assertFalse((self.source / "literate.project.json").exists())
        for name in manifest["components"]:
            binding = json.loads(
                (self.bundle / "components" / name / "binding.json").read_bytes()
            )
            self.assertEqual(binding["component"]["root"], name)
            self.assertEqual(binding["inventory"]["stages"][0]["cwd"], name)
            self.assertEqual(binding["stage"], "staged")
            self.assertEqual(binding["authority"], "original-source")
            self.assertEqual(binding["receipt_policy"]["minimum_test_count"], 1)
            path = self.bundle / "components" / name / "component.md"
            authoring = parse_component_markdown(
                path, path.read_text(encoding="utf-8"), project_root=self.bundle
            )
            self.assertEqual(authoring.coordinate.name, name)
        self.assertEqual(list(self.bundle.rglob("*.py")), [])

    def test_real_independent_receipts_and_shared_invalidation(self):
        self.stage()
        before = self.snapshot(self.source)
        kit, kit_result = self.run_component("kit")
        runtime, runtime_result = self.run_component("runtime")
        self.assertEqual(self.snapshot(self.source), before)
        self.assertNotEqual(
            kit_result["receipt"]["project_revision"],
            runtime_result["receipt"]["project_revision"],
        )
        self.assertEqual(self.check("kit", kit)["state"], "current")
        self.assertEqual(self.check("runtime", runtime)["state"], "current")
        (self.source / "kit" / "value.py").write_text("VALUE = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(
            MonorepoAdoptionError, "current Component revision"
        ):
            self.check("kit", kit)
        self.assertEqual(self.check("runtime", runtime)["state"], "current")
        replacement, _ = self.run_component("kit", self.base / "kit-new.json")
        self.assertEqual(self.check("kit", replacement)["state"], "current")
        (self.source / "shared" / "api.py").write_text("API = 2\n", encoding="utf-8")
        for name, receipt in (("kit", replacement), ("runtime", runtime)):
            with self.assertRaisesRegex(
                MonorepoAdoptionError, "current Component revision"
            ):
                self.check(name, receipt)

    def test_membership_changes_are_scoped_and_require_refresh(self):
        self.stage()
        before = components.component_retained_revision(
            self.source, self.bundle, "runtime"
        )
        (self.source / "kit" / "new.py").write_text("NEW = 1\n", encoding="utf-8")
        self.assertEqual(
            components.component_retained_revision(self.source, self.bundle, "runtime"),
            before,
        )
        with self.assertRaisesRegex(MonorepoAdoptionError, "membership changed"):
            components.component_retained_revision(self.source, self.bundle, "kit")
        (self.source / "shared" / "extra.py").write_text(
            "EXTRA = 1\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(MonorepoAdoptionError, "membership changed"):
            components.component_retained_revision(self.source, self.bundle, "runtime")

    def test_failed_or_skipped_or_empty_tests_never_publish_receipts(self):
        for case, body in (
            (
                "failed",
                "import unittest\nclass T(unittest.TestCase):\n"
                " def test_bad(self): self.fail('bad')\n",
            ),
            (
                "skipped",
                "import unittest\nclass T(unittest.TestCase):\n"
                " @unittest.skip('not run')\n def test_skip(self): pass\n",
            ),
            ("empty", "# no tests\n"),
        ):
            with self.subTest(case=case):
                bundle = self.base / case
                (self.source / "kit" / "test_value.py").write_text(
                    body, encoding="utf-8"
                )
                self.stage(bundle)
                output = self.base / f"{case}.json"
                with self.assertRaises(HarnessBaselineError):
                    components.run_component_retained_harness(
                        self.source,
                        bundle,
                        "kit",
                        output,
                        acknowledged=True,
                        worker_id="test",
                        timeout_seconds=30,
                    )
                self.assertFalse(output.exists())

    def test_acknowledgement_and_stale_plan_refuse_before_writes(self):
        with self.assertRaisesRegex(MonorepoAdoptionError, "acknowledgement"):
            components.stage_monorepo_components(
                self.source,
                self.selection_file,
                self.bundle,
                expected_plan_identity="invalid",
                acknowledged=False,
            )
        self.assertFalse(self.bundle.exists())
        self.selection_file.write_text(json.dumps(self.selection), encoding="utf-8")
        plan = plan_convert(
            self.source, default_branch="main", root_plan=self.selection_file
        )["root_refinement"]
        (self.source / "kit" / "value.py").write_text("VALUE = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(MonorepoAdoptionError, "plan changed"):
            components.stage_monorepo_components(
                self.source,
                self.selection_file,
                self.bundle,
                expected_plan_identity=plan["plan_identity"],
                acknowledged=True,
                default_branch="main",
            )
        self.assertFalse(self.bundle.exists())
        with self.assertRaisesRegex(MonorepoAdoptionError, "acknowledgement"):
            components.run_component_retained_harness(
                self.source,
                self.bundle,
                "kit",
                self.base / "never.json",
                acknowledged=False,
                worker_id="test",
            )

    def test_colliding_or_inside_source_destinations_are_preserved(self):
        self.bundle.mkdir()
        (self.bundle / "foreign").write_bytes(b"keep")
        with self.assertRaisesRegex(MonorepoAdoptionError, "must be new"):
            self.stage()
        self.assertEqual((self.bundle / "foreign").read_bytes(), b"keep")
        with self.assertRaisesRegex(MonorepoAdoptionError, "outside the source"):
            self.stage(self.source / "bundle")

    def test_failure_before_completion_manifest_rolls_back_owned_files(self):
        original = components._write_new

        def fail_manifest(path, content):
            if path.name == "bundle.json":
                raise OSError("injected publication failure")
            return original(path, content)

        with mock.patch.object(components, "_write_new", side_effect=fail_manifest):
            with self.assertRaisesRegex(OSError, "injected"):
                self.stage()
        self.assertFalse(self.bundle.exists())

    def test_rollback_preserves_concurrently_replaced_files(self):
        original = components._write_new

        def replace_then_fail(path, content):
            if path.name == "bundle.json":
                (self.bundle / "plan.json").write_bytes(b"foreign replacement")
                raise OSError("injected")
            return original(path, content)

        with mock.patch.object(components, "_write_new", side_effect=replace_then_fail):
            with self.assertRaises(OSError):
                self.stage()
        self.assertEqual(
            (self.bundle / "plan.json").read_bytes(), b"foreign replacement"
        )
        self.assertFalse((self.bundle / "bundle.json").exists())
        with self.assertRaises(MonorepoAdoptionError) as missing:
            components.component_retained_revision(self.source, self.bundle, "kit")
        self.assertEqual(missing.exception.code, "monorepo.bundle_invalid")

    def test_receipt_substitution_and_tampering_are_rejected(self):
        self.stage()
        path, result = self.run_component("kit")
        with self.assertRaises(MonorepoAdoptionError):
            self.check("runtime", path)
        for mutate in (
            lambda data: data["evidence"].update(native_generation=True),
            lambda data: data["evidence"]["phases"][0].update(command="different"),
            lambda data: data["evidence"]["phases"][0].update(timed_out=True),
            lambda data: data["evidence"]["phases"][0]["test_collection"].update(
                skipped=1
            ),
            lambda data: data["receipt"].update(result="sha256:" + "0" * 64),
        ):
            data = copy.deepcopy(result)
            mutate(data)
            path.write_bytes(canonical_json_bytes(data) + b"\n")
            with self.assertRaises((ValueError, MonorepoAdoptionError)):
                self.check("kit", path)

    def test_lifecycle_contention_refuses_before_execution(self):
        self.stage()
        with project_lifecycle_lock(self.bundle, operation="test-hold"):
            with mock.patch.object(components, "execute_retained_harness") as runner:
                with self.assertRaises(ProjectLifecycleLockError):
                    self.run_component("kit")
                runner.assert_not_called()

    def test_source_or_projection_drift_during_run_prevents_publication(self):
        self.stage()
        run = components.execute_retained_harness

        def change_source(*args, **kwargs):
            result = run(*args, **kwargs)
            (self.source / "kit" / "value.py").write_text(
                "VALUE = 99\n", encoding="utf-8"
            )
            return result

        with mock.patch.object(
            components, "execute_retained_harness", side_effect=change_source
        ):
            with self.assertRaisesRegex(
                MonorepoAdoptionError, "changed during execution"
            ):
                self.run_component("kit")
        self.assertFalse((self.base / "kit.receipt.json").exists())

    def test_indirect_input_refuses(self):
        self.stage()
        (self.source / "kit" / "value.py").unlink()
        try:
            (self.source / "kit" / "value.py").symlink_to(
                self.source / "runtime" / "value.py"
            )
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(MonorepoAdoptionError, "indirect custody"):
            self.run_component("kit")
        self.assertFalse((self.base / "kit.receipt.json").exists())

    def test_partial_write_failure_removes_only_the_new_file(self):
        with mock.patch.object(
            components.os, "fsync", side_effect=OSError("fsync failed")
        ):
            with self.assertRaisesRegex(OSError, "fsync failed"):
                self.stage()
        self.assertFalse(self.bundle.exists())

    def test_source_mutation_during_staging_rolls_back_the_bundle(self):
        write = components._write_new

        def mutate_source(path, content):
            identity = write(path, content)
            if path.name == "plan.json":
                (self.source / "kit" / "value.py").write_text(
                    "VALUE = 22\n", encoding="utf-8"
                )
            return identity

        with mock.patch.object(components, "_write_new", side_effect=mutate_source):
            with self.assertRaisesRegex(
                MonorepoAdoptionError, "changed during staging"
            ):
                self.stage()
        self.assertFalse(self.bundle.exists())
        self.assertEqual(
            (self.source / "kit" / "value.py").read_text(encoding="utf-8"),
            "VALUE = 22\n",
        )

    def test_component_projection_change_is_local_and_cannot_be_ignored(self):
        self.stage()
        before = components.component_retained_revision(
            self.source, self.bundle, "runtime"
        )
        (self.bundle / "components" / "kit" / "component.md").write_text(
            "changed", encoding="utf-8"
        )
        self.assertEqual(
            components.component_retained_revision(self.source, self.bundle, "runtime"),
            before,
        )
        with self.assertRaisesRegex(MonorepoAdoptionError, "projection changed"):
            components.component_retained_revision(self.source, self.bundle, "kit")

    def test_receipt_destination_collision_is_refused_before_execution(self):
        self.stage()
        destination = self.base / "kit.receipt.json"
        destination.write_bytes(b"foreign receipt")
        with mock.patch.object(components, "execute_retained_harness") as execute:
            with self.assertRaisesRegex(MonorepoAdoptionError, "must be new"):
                self.run_component("kit")
            execute.assert_not_called()
        self.assertEqual(destination.read_bytes(), b"foreign receipt")

    def test_harness_source_mutation_never_publishes_receipt(self):
        (self.source / "kit" / "test_value.py").write_text(
            "import pathlib\nimport unittest\nclass T(unittest.TestCase):\n"
            " def test_mutation(self):\n"
            "  pathlib.Path('value.py').write_text('changed')\n",
            encoding="utf-8",
        )
        self.stage()
        original = (self.source / "kit" / "value.py").read_bytes()
        with self.assertRaises(HarnessBaselineError):
            self.run_component("kit")
        self.assertFalse((self.base / "kit.receipt.json").exists())
        self.assertEqual((self.source / "kit" / "value.py").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
