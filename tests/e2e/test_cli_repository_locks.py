"""Public repository lock/plan/verify journeys without implicit host side effects."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from literate_ai.adapters.orchestration_planning import plan_orchestration
from literate_ai.adapters.project_initialization import (
    initialize_project,
    record_project_authority_review,
)
from literate_ai.adapters.repository_locks import RepositoryLockStore
from literate_ai.cli import dispatch, main
from literate_ai.contracts import RepositoryParentSelection
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.repository_orchestration import RepositoryOrchestration
from literate_ai.projects import ProjectConfigurationStore
from tests.support import fixtures_test_repository_lock_planning as fixtures
from tests.support.fixtures_test_repository_orchestration import git, snapshot
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


class RepositoryLockCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        registry = Registry().with_resources(
            (uri, Resource.from_contents(value, default_specification=DRAFT202012))
            for uri, value in SchemaCatalog().resources.items()
        )
        cls.validator = Draft202012Validator(
            {"$ref": "urn:literate-ai:schema:v2:repository-commands"}, registry=registry
        )

        # Build the git fixture repositories once; each test mutates a private copy.
        fixture = fixtures.RepositoryLockPlanningTests()
        fixture.setUp()
        template = tempfile.TemporaryDirectory()
        cls.addClassCleanup(template.cleanup)
        cls.template = Path(template.name).resolve() / "base"
        shutil.copytree(fixture.base, cls.template, symlinks=True)
        cls.root_name = fixture.root.relative_to(fixture.base)
        fixture.doCleanups()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve() / "base"
        shutil.copytree(self.template, self.base, symlinks=True)
        self.root = self.base / self.root_name

    def root_with_components(self):
        modules = (self.root / ".gitmodules").read_bytes()
        self.root = self.base / "mixed"
        initialize_project(
            self.root,
            flavor_selectors=("python", "macos", "bazel"),
            bootstrap_tools=False,
            parent_selection=RepositoryParentSelection.root(),
        )
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "test@example.test")
        git(self.root, "config", "user.name", "Test")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "root Components")
        pin = git(self.root, "rev-parse", "HEAD").decode().strip()
        (self.root / ".gitmodules").write_bytes(modules)
        git(self.root, "add", ".gitmodules")
        for path in ("app", "lib"):
            git(self.root, "update-index", "--add", "--cacheinfo", "160000", pin, path)
        git(self.root, "commit", "-q", "-m", "independent children")
        binding = RepositoryOrchestration.from_dict(
            plan_orchestration(self.root, self.base / "orchestration.json")[
                "repository_authority"
            ]
        )
        store = ProjectConfigurationStore(self.root)
        current = store.read()
        store.update(
            current,
            replace(current.definition, repository_orchestration=binding),
        )
        record_project_authority_review(self.root)

    def test_mixed_root_locks_components_and_inspects_without_a_writing_mutex(self):
        self.root_with_components()
        code, locked = self.invoke("lock")
        self.assertEqual(code, 0, locked)
        components = locked["result"]["components"]
        self.assertTrue(components)
        self.assertTrue(all(item["lock"]["state"] == "current" for item in components))
        before = snapshot(self.root)
        with patch(
            "literate_ai.adapters.component_lock_commands.ComponentLockStore.operation",
            side_effect=AssertionError("read-only operation acquired writing mutex"),
        ):
            for command, options in (
                ("plan", ()),
                ("lock", ("--check",)),
                ("lock", ("--diff",)),
                ("verify", ("--gate", "locks")),
            ):
                code, report = self.invoke(command, *options)
                self.assertEqual(code, 0, report)
                if command != "verify":
                    self.assertEqual(report["result"]["components"], components)
                    self.assertNotIn(str(self.root), json.dumps(report["result"]))
        self.assertEqual(snapshot(self.root), before)

        plan = self.invoke("plan")[1]["result"]
        # Stage only root authority, preserving intentionally uninitialized Gitlinks.
        git(self.root, "add", "--", ".", ":(exclude)app", ":(exclude)lib")
        git(self.root, "commit", "-q", "-m", "mixed root locks")
        clone = self.base / "mixed-clone"
        git(self.base, "clone", "-q", "--depth", "1", self.root.as_uri(), str(clone))
        before = snapshot(clone)
        code, cloned = self.invoke("plan", root=clone)
        self.assertEqual(code, 0, cloned)
        self.assertEqual(cloned["result"], plan)
        self.assertEqual(snapshot(clone), before)

    def invoke(self, command, *options, root=None, tty=False):
        output, errors = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            for name in (
                "maybe_host_self_update",
                "ensure_user_mcp_catalog",
                "journal_mutagenic_event",
                "_fan_out_operator_mcp_event",
                "_run_operator_mcp_discovery",
            ):
                stack.enter_context(
                    patch.object(dispatch, name, side_effect=AssertionError(name))
                )
            stack.enter_context(
                patch.object(
                    dispatch.PerformanceRecorder,
                    "span",
                    side_effect=AssertionError("telemetry"),
                )
            )
            stack.enter_context(
                patch(
                    "literate_ai.cli.generation._prepare_generation",
                    side_effect=AssertionError("generation"),
                )
            )
            if tty:
                stack.enter_context(patch.object(output, "isatty", return_value=True))
            code = main(
                [
                    command,
                    str(root or self.root),
                    *options,
                    *(() if tty else ("--json",)),
                ],
                stdout=output,
                stderr=errors,
            )
        text = output.getvalue() or errors.getvalue()
        if tty:
            return code, text
        report = json.loads(text)
        if command in {"lock", "plan"} and "result" in report:
            self.validator.validate(report["result"])
        return code, report

    def test_missing_and_stale_locks_refuse_planning_without_writes(self):
        before = snapshot(self.base)
        code, report = self.invoke("plan")
        self.assertEqual(code, 2, report)
        self.assertEqual(report["error"]["code"], "orchestration.lock_not_current")
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(self.invoke("lock")[0], 0)
        store = RepositoryLockStore(self.root)
        stale = replace(store.read(), project_id="old")
        store.path.write_bytes(canonical_json_bytes(stale.to_dict()) + b"\n")
        before = snapshot(self.base)
        code, difference = self.invoke("lock", "--diff")
        self.assertEqual(code, 1, difference)
        self.assertEqual(
            difference["result"]["previous_repository_lock"], stale.to_dict()
        )
        self.assertEqual(self.invoke("plan")[0], 2)
        self.assertEqual(self.invoke("verify", "--gate", "locks")[0], 1)
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(self.invoke("lock")[0], 0)
        self.assertEqual(self.invoke("plan")[0], 0)

    def test_index_drift_never_silently_refreshes_root_authority(self):
        self.assertEqual(self.invoke("lock")[0], 0)
        pin = git(self.root, "rev-parse", "HEAD").decode().strip()
        git(self.root, "update-index", "--cacheinfo", "160000", pin, "app")
        before = snapshot(self.base)
        for command in ("lock", "plan"):
            code, report = self.invoke(command)
            self.assertEqual(code, 2, report)
            self.assertEqual(report["error"]["code"], "orchestration.binding_stale")
        self.assertEqual(self.invoke("verify", "--gate", "locks")[0], 1)
        self.assertEqual(snapshot(self.base), before)
