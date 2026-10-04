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
from literate_ai.adapters.harness_inventory import HarnessBaselineError
from literate_ai.adapters.monorepo_adoption import (
    SELECTION_SCHEMA,
    MonorepoAdoptionError,
)
from literate_ai.adapters.project_initialization import plan_convert
from literate_ai.contracts import canonical_identity, canonical_json_bytes


class MonorepoComponentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Staging and running both real harnesses dominates this module; do it once
        # and give each test a private copy so no test can mutate the template.
        temporary = tempfile.TemporaryDirectory(prefix="litai component template ")
        cls.addClassCleanup(temporary.cleanup)
        template = cls.__new__(cls)
        template._init_source(Path(temporary.name).resolve())
        manifest = template.stage()
        receipts = {
            name: template.run_component(name)[0] for name in ("kit", "runtime")
        }
        cls._template = {
            "base": template.base,
            "manifest": manifest,
            "receipts": receipts,
        }

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="litai component receipts ")
        self.addCleanup(temporary.cleanup)
        self._init_source(Path(temporary.name).resolve())

    def _init_source(self, base):
        self.base = base
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
        """Use a private copy of the class's staged bundle and real receipts."""

        template = type(self)._template
        shutil.copytree(template["base"] / "source", self.source, dirs_exist_ok=True)
        shutil.copytree(template["base"] / "bundle", self.bundle)
        shutil.copy2(template["base"] / "selection.json", self.selection_file)
        self.manifest = copy.deepcopy(template["manifest"])
        self.receipts = {}
        for name, path in template["receipts"].items():
            self.receipts[name] = self.base / path.name
            shutil.copy2(path, self.receipts[name])

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

    def test_receipt_substitution_and_tampering_are_rejected(self):
        self.retained_bundle()
        path = self.receipts["kit"]
        result = json.loads(path.read_bytes())
        self.check("kit", path)
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
