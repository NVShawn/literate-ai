"""Real Git and subprocess qualification of retained-child delegation."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import repository_lifecycle
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
from literate_ai.cli import dispatch, main
from literate_ai.contracts.repository_lifecycle import SCHEMA, RepositoryLifecycle
from literate_ai.contracts.repository_orchestration import (
    RepositoryOrchestration,
    RepositoryPin,
)
from tests.support.fixtures_test_repository_orchestration import git, repository


class RepositoryLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "root"
        repository(self.root)
        for name in ("lib", "app"):
            child = self.root / name
            repository(child)
            (child / ".gitignore").write_text("_build/\n")
            git(child, "add", ".gitignore")
            git(child, "commit", "-qm", "ignore outputs")
            pin = git(child, "rev-parse", "HEAD").decode().strip()
            git(self.root, "update-index", "--add", "--cacheinfo", "160000", pin, name)
        (self.root / ".gitmodules").write_text(
            '[submodule "lib"]\npath = lib\nurl = ../lib.git\n'
            '[submodule "app"]\npath = app\nurl = ../app.git\n'
        )
        (self.root / ".gitignore").write_text("_build/\n")
        git(self.root, "add", ".gitmodules", ".gitignore")
        git(self.root, "commit", "-qm", "children")
        inventory = inspect_gitlink_inventory(self.root)
        authority = RepositoryOrchestration(
            inventory.gitmodules_identity,
            tuple(
                RepositoryPin(c.name, c.path, c.url, c.commit, c.branch)
                for c in inventory.children
            ),
            (),
        )
        (self.root / "literate.project.json").write_text(
            json.dumps({"repository_orchestration": authority.to_dict()})
        )
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

    @unittest.skipIf(os.name == "nt", "Windows does not rename an open CRT file")
    def test_replaced_execution_marker_is_preserved_and_blocks_consumers(self):
        marker = self.root / "_build/lifecycle/execution.lock"
        foreign = b'{"owner":"replacement"}\n'
        execute = repository_lifecycle._execute

        def replace_marker(*args):
            code = execute(*args)
            replacement = marker.with_suffix(".replacement")
            replacement.write_bytes(foreign)
            os.replace(replacement, marker)
            return code

        with patch.object(repository_lifecycle, "_execute", replace_marker):
            with self.assertRaises(OrchestrationInventoryError):
                self.run_plan()
        self.assertEqual(marker.read_bytes(), foreign)
        self.assertFalse((self.root / "app/_build").exists())

    @unittest.skipIf(os.name == "nt", "Windows does not rename an open CRT file")
    def test_same_bytes_replacement_is_not_retired(self):
        self._assert_changed_marker_preserved(replacement=True)

    def test_edited_execution_marker_is_not_retired(self):
        self._assert_changed_marker_preserved(replacement=False)

    def _assert_changed_marker_preserved(self, *, replacement):
        marker = self.root / "_build/lifecycle/execution.lock"
        execute = repository_lifecycle._execute
        expected = []

        def change_marker(*args):
            code = execute(*args)
            if replacement:
                expected.append(marker.read_bytes())
                other = marker.with_suffix(".replacement")
                other.write_bytes(expected[-1])
                os.replace(other, marker)
            else:
                expected.append(b"modified foreign owner\n")
                marker.write_bytes(expected[-1])
            return code

        with patch.object(repository_lifecycle, "_execute", change_marker):
            with self.assertRaises(OrchestrationInventoryError):
                self.run_plan()
        self.assertEqual(marker.read_bytes(), expected[-1])
        self.assertFalse((self.root / "app/_build").exists())

    def test_receipt_setup_failure_releases_own_execution_marker(self):
        marker = self.root / "_build/lifecycle/execution.lock"
        with patch.object(
            repository_lifecycle, "_write", side_effect=OSError("disk full")
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.run_plan()
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / "lib/_build").exists())

    def test_elapsed_time_accumulates_commands_on_success_and_failure(self):
        recipe = self.contract["children"][1]["operations"]["build"]
        first = recipe["commands"][0]
        execute = repository_lifecycle._execute
        for returncode in (0, 17):
            with self.subTest(returncode=returncode):
                recipe["commands"] = [
                    first,
                    [sys.executable, "-c", f"raise SystemExit({returncode})"],
                ]
                self.declare()
                clock = [100.0]

                def timed_execute(*args, clock=clock):
                    result = execute(*args)
                    clock[0] += 2.0
                    return result

                with (
                    patch.object(
                        repository_lifecycle.time,
                        "monotonic",
                        lambda clock=clock: clock[0],
                    ),
                    patch.object(repository_lifecycle, "_execute", timed_execute),
                ):
                    receipt = self.run_plan()
                self.assertEqual(receipt["nodes"][0]["elapsed_seconds"], 4.0)
                self.assertEqual(receipt["nodes"][0]["returncode"], returncode)
                self.assertEqual(
                    receipt["status"], "failed" if returncode else "passed"
                )

    def test_missing_operation_refuses_before_any_execution(self):
        self.contract["children"][0]["operations"]["build"] = None
        self.declare()
        with self.assertRaisesRegex(OrchestrationInventoryError, "not implemented"):
            self.run_plan()
        self.assertFalse((self.root / "lib/_build").exists())

    def test_missing_product_cannot_pass(self):
        self.contract["children"][1]["operations"]["build"]["outputs"] = ["missing"]
        self.declare()
        with self.assertRaises(OSError):
            self.run_plan()
        receipt = json.loads(
            next((self.root / "_build/lifecycle").glob("*/receipt.json")).read_text()
        )
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["nodes"][1]["status"], "blocked")

    def test_dirty_child_and_changed_plan_refuse(self):
        plan = self.plan()
        (self.root / "lib/source.txt").write_text("local work")
        with self.assertRaisesRegex(OrchestrationInventoryError, "local changes"):
            self.run_plan(plan)
        self.assertEqual((self.root / "lib/source.txt").read_text(), "local work")

    def test_changed_recipe_refuses_exact_plan(self):
        plan = self.plan()
        self.contract["children"][0]["operations"]["build"]["timeout_seconds"] = 10
        self.declare()
        with self.assertRaisesRegex(OrchestrationInventoryError, "stale"):
            self.run_plan(plan)

    def test_changed_root_dockerfile_refuses_exact_plan(self):
        dockerfile = self.root / "Dockerfile"
        dockerfile.write_text("FROM scratch\n")
        self.contract["inputs"] = ["Dockerfile"]
        self.declare()
        plan = self.plan()
        dockerfile.write_text("FROM different\n")
        with self.assertRaisesRegex(OrchestrationInventoryError, "stale"):
            self.run_plan(plan)

    def test_contract_refuses_cycles_unknown_children_and_escape(self):
        for mutate in (
            lambda c: c["children"][1]["dependencies"].append("app"),
            lambda c: c["children"][1]["dependencies"].append("missing"),
            lambda c: c["children"][1]["operations"]["build"].update(cwd="../app"),
        ):
            value = copy.deepcopy(self.contract)
            mutate(value)
            with self.assertRaises(ValueError):
                RepositoryLifecycle.from_dict(value).ordered()

    def test_inventory_coverage_required_even_for_subset(self):
        self.contract["children"] = self.contract["children"][1:]
        self.declare()
        with self.assertRaisesRegex(
            OrchestrationInventoryError, "every indexed Gitlink"
        ):
            self.plan(selected=("lib",))

    def test_cli_plan_has_no_implicit_host_updates(self):
        output = io.StringIO()
        with patch.object(
            dispatch, "maybe_host_self_update", side_effect=AssertionError("update")
        ):
            code = main(
                [
                    "--json",
                    "orchestrate",
                    "plan",
                    "build",
                    str(self.root),
                    "--declaration",
                    str(self.declaration),
                ],
                stdout=output,
            )
        self.assertEqual(code, 0, output.getvalue())
        self.assertTrue(json.loads(output.getvalue())["ok"])

    def test_timeout_retains_failed_receipt(self):
        recipe = self.contract["children"][1]["operations"]["build"]
        recipe.update(
            commands=[
                [
                    sys.executable,
                    "-c",
                    "import time; print('started', flush=True); time.sleep(60)",
                ]
            ],
            timeout_seconds=1,
        )
        self.declare()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_plan()
        receipt = json.loads(
            next((self.root / "_build/lifecycle").glob("*/receipt.json")).read_text()
        )
        self.assertEqual(receipt["status"], "failed")
        self.assertGreaterEqual(receipt["nodes"][0]["elapsed_seconds"], 1.0)
        self.assertIn("started", Path(receipt["nodes"][0]["logs"][0]).read_text())
        self.assertFalse((self.root / "_build/lifecycle/execution.lock").exists())

    def test_hydrated_lfs_is_clean_without_running_global_filter(self):
        child = self.root / "lib"
        payload = b"real binary product\x00\xff"
        pointer = (
            "version https://git-lfs.github.com/spec/v1\n"
            f"oid sha256:{hashlib.sha256(payload).hexdigest()}\n"
            f"size {len(payload)}\n"
        )
        # LFS pointers use canonical LF even on Windows; -text below prevents
        # Git from normalizing a fixture written with platform CRLF newlines.
        (child / "binary.dat").write_bytes(pointer.encode("ascii"))
        (child / ".gitattributes").write_text("binary.dat filter=lfs -text\n")
        git(child, "add", "binary.dat", ".gitattributes")
        git(child, "commit", "-qm", "LFS pointer")
        (child / "binary.dat").write_bytes(payload)
        self.assertTrue(_clean_child(child))
        (child / "binary.dat").write_bytes(b"corrupt binary data")
        self.assertFalse(_clean_child(child))

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
