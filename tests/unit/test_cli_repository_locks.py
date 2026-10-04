"""Public repository lock/plan/verify journeys without implicit host side effects."""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import ExitStack
from dataclasses import replace
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

    def setUp(self):
        fixture = fixtures.RepositoryLockPlanningTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root, self.base = fixture.root, fixture.base

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

    def test_mixed_root_missing_component_lock_refuses_plan_and_check(self):
        self.root_with_components()
        code, locked = self.invoke("lock")
        self.assertEqual(code, 0, locked)
        component = self.root / locked["result"]["components"][0]["component"]
        (component / "component.lock.json").unlink()
        before = snapshot(self.root)
        code, checked = self.invoke("lock", "--check")
        self.assertEqual(code, 1, checked)
        self.assertEqual(checked["result"]["lock"]["state"], "current")
        self.assertEqual(checked["result"]["components"][0]["lock"]["state"], "missing")
        code, text = self.invoke("lock", "--check", tty=True)
        self.assertEqual(code, 1, text)
        self.assertIn("repository lock: not current", text)
        self.assertIn("Repository artifact: current", text)
        code, plan = self.invoke("plan")
        self.assertEqual(code, 2, plan)
        self.assertEqual(plan["error"]["code"], "orchestration.lock_not_current")
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(self.invoke("lock")[0], 0)
        self.assertEqual(self.invoke("plan")[0], 0)
        (component / "component.lock.json").write_bytes(b"{}\n")
        before = snapshot(self.root)
        self.assertEqual(self.invoke("verify", "--gate", "locks")[0], 1)
        self.assertEqual(self.invoke("plan")[0], 2)
        self.assertEqual(snapshot(self.root), before)

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

    def test_public_schema_rejects_unknown_fields_and_execution_claims(self):
        self.assertEqual(self.invoke("lock")[0], 0)
        for command in ("lock", "plan"):
            report = self.invoke(command)[1]["result"]
            for update in (
                {"execution": True},
                {"publication": "verified"},
                {"unknown": "field"},
                {"repository_lock_identity": "latest"},
            ):
                self.assertTrue(list(self.validator.iter_errors({**report, **update})))
            for field in report:
                incomplete = {
                    key: value for key, value in report.items() if key != field
                }
                self.assertTrue(list(self.validator.iter_errors(incomplete)), field)

    def test_public_lock_plan_verify_and_check_are_root_only(self):
        before = snapshot(self.base)
        code, missing = self.invoke("lock", "--check")
        self.assertEqual(code, 1, missing)
        self.assertEqual(missing["result"]["lock"]["state"], "missing")
        self.assertEqual(self.invoke("verify", "--gate", "locks")[0], 1)
        self.assertEqual(snapshot(self.base), before)
        code, created = self.invoke("lock")
        self.assertEqual(code, 0, created)
        self.assertTrue(created["result"]["repository_lock_updated"])
        self.assertEqual(created["result"]["components"], [])
        after = snapshot(self.base)
        self.assertEqual({path: after[path] for path in before}, before)
        self.assertEqual(
            set(after) - set(before), {"super/.literate/repository.lock.json"}
        )
        code, plan = self.invoke("plan")
        self.assertEqual(code, 0, plan)
        self.assertEqual(plan["result"]["schema"], "literate-ai/repository-plan@1")
        self.assertFalse(plan["result"]["writes"])
        self.assertFalse(plan["result"]["execution"])
        self.assertEqual(plan["result"]["publication"], "not-checked")
        self.assertEqual(self.invoke("lock", "--check")[0], 0)
        self.assertEqual(self.invoke("verify", "--gate", "locks")[0], 0)
        code, noop = self.invoke("lock")
        self.assertEqual(code, 0, noop)
        self.assertFalse(noop["result"]["repository_lock_updated"])
        self.assertEqual(snapshot(self.base), after)

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

    def test_clean_clone_reuses_committed_lock_and_plan_identity(self):
        self.assertEqual(self.invoke("lock")[0], 0)
        plan = self.invoke("plan")[1]["result"]
        git(
            self.root,
            "add",
            "SKILL.md",
            "PROJECT.md",
            "literate.project.json",
            ".literate",
        )
        git(self.root, "commit", "-q", "-m", "root locks")
        clone = self.base / "clone"
        git(self.base, "clone", "-q", "--depth", "1", self.root.as_uri(), str(clone))
        before = snapshot(clone)
        code, reconstructed = self.invoke("plan", root=clone)
        self.assertEqual(code, 0, reconstructed)
        self.assertEqual(reconstructed["result"], plan)
        self.assertEqual(self.invoke("lock", "--check", root=clone)[0], 0)
        self.assertEqual(snapshot(clone), before)

    def test_provider_and_meaningless_component_options_refuse(self):
        for command, options in (
            ("plan", ("--model", "never-call")),
            ("plan", ("--recipe-id", "not-a-generation-plan")),
            ("lock", ("--large-review", "start")),
            ("lock", ("--target", "other")),
            ("plan", ("--flavor", "+python")),
            ("lock", ("--flavor-root", "unused")),
        ):
            with self.subTest(command=command, options=options):
                before = snapshot(self.base)
                code, report = self.invoke(command, *options)
                self.assertEqual(code, 2, report)
                self.assertEqual(
                    report["error"]["code"], "orchestration.component_options"
                )
                self.assertEqual(snapshot(self.base), before)

    def test_matrix_cell_override_refuses_before_creation(self):
        directory = self.base / "matrix"
        before = snapshot(self.base)
        with patch.dict(os.environ, {"LITAI_MATRIX_CELL_ROOT": str(directory)}):
            for command in ("lock", "plan"):
                code, report = self.invoke(command)
                self.assertEqual(code, 2, report)
                self.assertEqual(
                    report["error"]["code"], "orchestration.component_options"
                )
            self.assertEqual(self.invoke("verify", "--gate", "locks")[0], 1)
        self.assertFalse(directory.exists())
        self.assertEqual(snapshot(self.base), before)

    def test_explicit_side_effect_options_refuse_without_implicit_side_effects(self):
        for command in ("lock", "plan", "verify"):
            for options in (
                ("--discover-mcps",),
                ("--debug", str(self.base / "debug.json")),
            ):
                with self.subTest(command=command, options=options):
                    before = snapshot(self.base)
                    code, report = self.invoke(command, *options)
                    self.assertEqual(code, 2, report)
                    self.assertEqual(
                        report["error"]["code"], "orchestration.read_only_options"
                    )
                    self.assertEqual(snapshot(self.base), before)

    def test_child_invalid_authority_is_not_admitted(self):
        child = self.root / "app"
        child.mkdir()
        (child / "literate.project.json").write_bytes(b"independent invalid authority")
        self.assertEqual(self.invoke("lock")[0], 0)
        before = snapshot(self.base)
        code, plan = self.invoke("plan")
        self.assertEqual(code, 0, plan)
        self.assertEqual(plan["result"]["components"], [])
        self.assertEqual(snapshot(self.base), before)

    def test_root_selection_uses_declared_catalogs_not_an_incidental_component(self):
        (self.root / "component.md").write_text(
            "Not declared in the root's Component catalogs.\n", encoding="utf-8"
        )
        code, locked = self.invoke("lock")
        self.assertEqual(code, 0, locked)
        self.assertEqual(locked["result"]["components"], [])
        before = snapshot(self.base)
        self.assertEqual(self.invoke("plan")[0], 0)
        self.assertEqual(snapshot(self.base), before)

    def test_interactive_output_preserves_exact_pins_and_boundaries(self):
        code, text = self.invoke("lock", tty=True)
        self.assertEqual(code, 0, text)
        for pin in (
            RepositoryLockStore(self.root).read().repository_orchestration.repositories
        ):
            self.assertIn(pin.commit, text)
        code, text = self.invoke("plan", tty=True)
        self.assertEqual(code, 0, text)
        self.assertIn("Read-only", text)
        self.assertIn("No child execution", text)
