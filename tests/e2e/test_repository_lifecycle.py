"""Real Git and subprocess qualification of retained-child delegation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from literate_ai.adapters.repository_lifecycle import (
    _clean_child,
    _target,
    plan_repository_lifecycle,
    run_repository_lifecycle,
)
from literate_ai.adapters.repository_orchestration import (
    OrchestrationInventoryError,
    inspect_gitlink_inventory,
)
from literate_ai.contracts.repository_lifecycle import SCHEMA
from literate_ai.contracts.repository_orchestration import (
    RepositoryOrchestration,
    RepositoryPin,
)
from tests.support.fixtures_test_repository_orchestration import git, repository


class RepositoryLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Build the Git fixture once; every test works on its own copy so the
        # template is never mutated.
        template = tempfile.TemporaryDirectory()
        cls.addClassCleanup(template.cleanup)
        cls.template = Path(template.name).resolve() / "root"
        root = cls.template
        repository(root)
        for name in ("lib", "app"):
            child = root / name
            repository(child)
            (child / ".gitignore").write_text("_build/\n")
            git(child, "add", ".gitignore")
            git(child, "commit", "-qm", "ignore outputs")
            pin = git(child, "rev-parse", "HEAD").decode().strip()
            git(root, "update-index", "--add", "--cacheinfo", "160000", pin, name)
        (root / ".gitmodules").write_text(
            '[submodule "lib"]\npath = lib\nurl = ../lib.git\n'
            '[submodule "app"]\npath = app\nurl = ../app.git\n'
        )
        (root / ".gitignore").write_text("_build/\n")
        git(root, "add", ".gitmodules", ".gitignore")
        git(root, "commit", "-qm", "children")
        inventory = inspect_gitlink_inventory(root)
        authority = RepositoryOrchestration(
            inventory.gitmodules_identity,
            tuple(
                RepositoryPin(c.name, c.path, c.url, c.commit, c.branch)
                for c in inventory.children
            ),
            (),
        )
        (root / "literate.project.json").write_text(
            json.dumps({"repository_orchestration": authority.to_dict()})
        )

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "root"
        shutil.copytree(self.template, self.root, symlinks=True)
        prefix = "from pathlib import Path; p=Path('_build'); p.mkdir(exist_ok=True); "
        lib = prefix + "(p/'value').write_text('library')"
        app = prefix + (
            "(p/'value').write_text("
            "Path('../lib/_build/value').read_text()+'-consumer')"
        )
        self.contract = {
            "schema": SCHEMA,
            "target": _target(),
            "inputs": [],
            "children": [self.child("app", app, ["lib"]), self.child("lib", lib, [])],
        }
        self.declaration = self.root / "lifecycle.json"
        self.declare()

    def child(self, name, script, dependencies):
        recipe = {
            "cwd": ".",
            "commands": [[sys.executable, "-c", script]],
            "outputs": ["_build/value"],
            "timeout_seconds": 20,
            "environment": {},
        }
        return {
            "path": name,
            "role": "source",
            "dependencies": dependencies,
            "operations": {"build": recipe, "package": None, "containerize": None},
        }

    def declare(self):
        self.declaration.write_text(json.dumps(self.contract))

    def plan(self, **kwargs):
        return plan_repository_lifecycle(self.root, self.declaration, "build", **kwargs)

    def run_plan(self, plan=None, **kwargs):
        return run_repository_lifecycle(
            self.root,
            self.declaration,
            "build",
            expected_plan_identity=(plan or self.plan())["plan_identity"],
            acknowledged=True,
            **kwargs,
        )

    def test_real_dependency_order_and_verified_resume(self):
        plan = self.plan()
        self.assertEqual([node["path"] for node in plan["nodes"]], ["lib", "app"])
        self.assertFalse((self.root / "lib/_build").exists())
        receipt = self.run_plan(plan)
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(
            (self.root / "app/_build/value").read_text(), "library-consumer"
        )
        resumed = self.run_plan(resume=Path(receipt["receipt"]))
        self.assertEqual(
            [node["status"] for node in resumed["nodes"]], ["reused", "reused"]
        )
        (self.root / "lib/_build/value").write_text("corrupt")
        repaired = self.run_plan(resume=Path(receipt["receipt"]))
        self.assertEqual(
            [node["status"] for node in repaired["nodes"]], ["passed", "passed"]
        )
        self.assertEqual(
            (self.root / "app/_build/value").read_text(), "library-consumer"
        )

    def test_command_failure_retains_logs_and_blocks_consumers(self):
        self.contract["children"][1]["operations"]["build"]["commands"] = [
            [
                sys.executable,
                "-c",
                "import sys; print('deliberate failure', flush=True); sys.exit(17)",
            ]
        ]
        self.declare()
        receipt = self.run_plan()
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["nodes"][0]["returncode"], 17)
        self.assertEqual(receipt["nodes"][1]["status"], "blocked")
        self.assertIn(
            "deliberate failure", Path(receipt["nodes"][0]["logs"][0]).read_text()
        )
        self.assertFalse((self.root / "app/_build").exists())

    def test_dirty_child_and_changed_plan_refuse(self):
        plan = self.plan()
        (self.root / "lib/source.txt").write_text("local work")
        with self.assertRaisesRegex(OrchestrationInventoryError, "local changes"):
            self.run_plan(plan)
        self.assertEqual((self.root / "lib/source.txt").read_text(), "local work")

    def test_planning_refuses_local_filters_before_status_can_execute_them(self):
        child = self.root / "lib"
        git(child, "config", "filter.fixture.clean", "must-not-run")
        with self.assertRaises(OrchestrationInventoryError) as caught:
            _clean_child(child)
        self.assertIn("external filters", str(caught.exception))

    @unittest.skipUnless(os.name == "posix", "SIGTERM process-group qualification")
    def test_sigterm_cancels_owned_grandchild_and_records_receipt(self):
        child = self.root / "lib"
        recipe = self.contract["children"][1]["operations"]["build"]
        grandchild = (
            "import time; from pathlib import Path; time.sleep(3); "
            "Path('_build/survived').write_text('unexpected')"
        )
        script = (
            "import subprocess,sys,time; from pathlib import Path; "
            "Path('_build').mkdir(exist_ok=True); "
            f"subprocess.Popen([sys.executable,'-c',{grandchild!r}]); "
            "Path('_build/started').write_text('ready'); time.sleep(60)"
        )
        recipe["commands"] = [[sys.executable, "-c", script]]
        self.declare()
        runner = (
            "from pathlib import Path; "
            "from literate_ai.adapters.repository_lifecycle import "
            "plan_repository_lifecycle as plan,run_repository_lifecycle as run; "
            f"root=Path({str(self.root)!r}); declaration=root/'lifecycle.json'; "
            "p=plan(root,declaration,'build'); "
            "run(root,declaration,'build',"
            "expected_plan_identity=p['plan_identity'],acknowledged=True)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", runner],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 15
            while not (child / "_build/started").exists():
                if time.monotonic() > deadline or process.poll() is not None:
                    self.fail("child command did not start")
                time.sleep(0.05)
            process.terminate()
            self.assertNotEqual(process.wait(timeout=10), 0)
            time.sleep(3.2)
            self.assertFalse((child / "_build/survived").exists())
            receipt = json.loads(
                next(
                    (self.root / "_build/lifecycle").glob("*/receipt.json")
                ).read_text()
            )
            self.assertEqual(receipt["status"], "cancelled")
            self.assertEqual(receipt["nodes"][1]["status"], "blocked")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
