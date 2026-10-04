from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_orchestration_planning``."""

import io
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import orchestration_planning as planning
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.cli import dispatch, main
from literate_ai.contracts.repository_orchestration import RepositoryOrchestration
from tests.support.fixtures_test_repository_orchestration import (
    git,
    repository,
    snapshot,
)


class OrchestrationPlanningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "super"
        repository(self.root)
        self.pin = git(self.root, "rev-parse", "HEAD").decode().strip()
        (self.root / ".gitmodules").write_text(
            '[submodule "app"]\npath = app\nurl = ../app.git\n'
            '[submodule "lib"]\npath = lib\nurl = git@example.test:group/lib.git\n',
            encoding="utf-8",
        )
        git(self.root, "add", ".gitmodules")
        for path in ("app", "lib"):
            git(
                self.root,
                "update-index",
                "--add",
                "--cacheinfo",
                "160000",
                self.pin,
                path,
            )
        git(self.root, "commit", "-q", "-m", "children")
        self.declaration = self.base / "orchestration.json"
        self.declare([{"consumer": "app", "provider": "lib"}])

    def declare(self, relationships):
        self.declaration.write_text(
            json.dumps(
                {
                    "schema": planning.DECLARATION_SCHEMA,
                    "relationships": relationships,
                }
            ),
            encoding="utf-8",
        )

    def invoke(self, operation, *options):
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
                    "literate_ai.cli.operator.init_project_from_args",
                    side_effect=AssertionError("flat conversion"),
                )
            )
            code = main(
                [
                    "onboard",
                    "orchestrate",
                    operation,
                    str(self.root),
                    "--declaration",
                    str(self.declaration),
                    "--json",
                    *options,
                ],
                stdout=output,
                stderr=errors,
            )
        return code, json.loads(output.getvalue() if code == 0 else errors.getvalue())

    def test_public_plan_and_check_bind_exact_inputs_without_writes(self):
        before = snapshot(self.base)
        code, envelope = self.invoke("plan")
        self.assertEqual(code, 0, envelope)
        plan = envelope["result"]
        self.assertEqual(
            plan["relationships"], [{"consumer": "app", "provider": "lib"}]
        )
        self.assertEqual(
            [child["path"] for child in plan["inventory"]["children"]], ["app", "lib"]
        )
        authority = RepositoryOrchestration.from_dict(plan["repository_authority"])
        self.assertEqual(authority.identity, plan["repository_authority_identity"])
        self.assertEqual(
            authority.gitmodules_identity, plan["inventory"]["gitmodules_identity"]
        )
        self.assertEqual(
            [pin.to_dict() for pin in authority.repositories],
            [
                {
                    "name": "app",
                    "path": "app",
                    "url": "../app.git",
                    "commit": self.pin,
                    "branch": None,
                },
                {
                    "name": "lib",
                    "path": "lib",
                    "url": "git@example.test:group/lib.git",
                    "commit": self.pin,
                    "branch": None,
                },
            ],
        )
        self.assertEqual(
            [edge.to_dict() for edge in authority.relationships], plan["relationships"]
        )
        self.assertEqual(plan["child_authority"], "independent")
        self.assertEqual(plan["publication"], "not-checked")
        for field in (
            "writes",
            "execution",
            "apply_supported",
            "initialization_performed",
        ):
            self.assertIs(plan[field], False)
        code, checked = self.invoke(
            "check", "--expected-plan-identity", plan["plan_identity"]
        )
        self.assertEqual(code, 0, checked)
        self.assertEqual(checked["result"]["state"], "current")
        self.assertEqual(snapshot(self.base), before)

    def test_identity_excludes_local_absolute_paths_and_clean_clone_location(self):
        first = planning.plan_orchestration(self.root, self.declaration)
        clone = self.base / "clone"
        git(self.base, "clone", "--depth", "1", self.root.as_uri(), str(clone))
        second_declaration = self.base / "another.json"
        second_declaration.write_bytes(self.declaration.read_bytes())
        second = planning.plan_orchestration(clone, second_declaration)
        self.assertEqual(first, second)
        self.assertNotIn(str(self.base), json.dumps(first))

    def test_changed_pin_declaration_or_initialization_stales_reviewed_plan(self):
        for change in ("pin", "declaration", "initialization"):
            with self.subTest(change=change):
                plan = planning.plan_orchestration(self.root, self.declaration)
                if change == "pin":
                    git(
                        self.root,
                        "update-index",
                        "--cacheinfo",
                        "160000",
                        "f" * 40,
                        "app",
                    )
                elif change == "declaration":
                    self.declare([])
                else:
                    repository(self.root / "app")
                before = snapshot(self.base)
                code, envelope = self.invoke(
                    "check", "--expected-plan-identity", plan["plan_identity"]
                )
                self.assertEqual(code, 2)
                self.assertEqual(envelope["error"]["code"], "orchestration.plan_stale")
                self.assertEqual(snapshot(self.base), before)

    def test_exact_declaration_bytes_are_reviewed_even_when_semantics_match(self):
        first = planning.plan_orchestration(self.root, self.declaration)
        with self.declaration.open("a", encoding="utf-8") as stream:
            stream.write("\n")
        second = planning.plan_orchestration(self.root, self.declaration)
        self.assertEqual(first["relationships"], second["relationships"])
        self.assertNotEqual(first["plan_identity"], second["plan_identity"])

    def test_persistent_authority_excludes_local_child_checkout_state(self):
        first = planning.plan_orchestration(self.root, self.declaration)
        repository(self.root / "app")
        second = planning.plan_orchestration(self.root, self.declaration)
        self.assertNotEqual(first["plan_identity"], second["plan_identity"])
        self.assertEqual(first["repository_authority"], second["repository_authority"])
        self.assertEqual(
            first["repository_authority_identity"],
            second["repository_authority_identity"],
        )

    def test_explicit_empty_relationships_are_valid_but_not_inferred(self):
        self.declare([])
        self.assertEqual(
            planning.plan_orchestration(self.root, self.declaration)["relationships"],
            [],
        )

    def test_bad_relationships_and_unknown_fields_refuse_without_writes(self):
        cases = (
            [{"consumer": "app", "provider": "unknown"}],
            [{"consumer": "app", "provider": "APP"}],
            [{"consumer": "app", "provider": "app"}],
            [{"consumer": "app", "provider": "lib"}] * 2,
            [{"consumer": "app", "provider": "lib", "execute": "untrusted"}],
            [{"consumer": "app", "provider": 1}],
            None,
        )
        for relationships in cases:
            with self.subTest(relationships=relationships):
                self.declare(relationships)
                before = snapshot(self.base)
                code, _ = self.invoke("plan")
                self.assertEqual(code, 2)
                self.assertEqual(snapshot(self.base), before)

    def test_invalid_duplicate_missing_and_oversized_declarations_refuse(self):
        for raw in (
            b"{",
            b"{}",
            b"[]",
            b"null",
            b"\xff",
            b'{"schema":"literate-ai/orchestration@1","relationships":[],"extra":true}',
            b'{"schema":"literate-ai/orchestration@1","relationships":[],"relationships":[]}',
            b"[" * 2000,
            b" " * (256 * 1024 + 1),
        ):
            with self.subTest(raw_length=len(raw)):
                self.declaration.write_bytes(raw)
                before = snapshot(self.base)
                code, envelope = self.invoke("plan")
                self.assertEqual(code, 2)
                self.assertEqual(
                    envelope["error"]["code"], "orchestration.declaration_invalid"
                )
                self.assertEqual(snapshot(self.base), before)
        self.declaration.unlink()
        self.assertEqual(self.invoke("plan")[0], 2)

    def test_indirect_declaration_refuses(self):
        original = self.base / "original.json"
        self.declaration.rename(original)
        try:
            self.declaration.symlink_to(original)
        except OSError:
            self.skipTest("host does not permit symlink fixture creation")
        self.assertEqual(self.invoke("plan")[0], 2)

    def test_check_requires_valid_reviewed_identity(self):
        for identity in ("not-an-identity", "sha256:" + "A" * 64, "sha256:" + "0" * 64):
            with self.subTest(identity=identity):
                code, envelope = self.invoke(
                    "check", "--expected-plan-identity", identity
                )
                self.assertEqual(code, 2)
                self.assertTrue(
                    envelope["error"]["code"].startswith("orchestration.plan_")
                )
        self.assertEqual(self.invoke("check")[0], 2)

    def test_apply_discovery_and_debug_file_flags_refuse_before_side_effects(self):
        before = snapshot(self.base)
        for options in (
            ("--apply", "--acknowledge"),
            ("--discover-mcps",),
            ("--debug=" + str(self.base / "debug.json"),),
        ):
            with self.subTest(options=options):
                self.assertEqual(self.invoke("plan", *options)[0], 2)
        self.assertEqual(snapshot(self.base), before)

    def test_concurrent_declaration_change_refuses(self):
        real = planning.inspect_gitlink_inventory

        def change(root):
            result = real(root)
            self.declare([])
            return result

        with patch.object(planning, "inspect_gitlink_inventory", side_effect=change):
            with self.assertRaises(OrchestrationInventoryError) as caught:
                planning.plan_orchestration(self.root, self.declaration)
        self.assertEqual(caught.exception.code, "orchestration.inputs_changed")

    def test_tty_result_does_not_suggest_unimplemented_apply(self):
        from tests.support.fixtures_test_operator_adoption import TtyStringIO

        output = TtyStringIO()
        errors = io.StringIO()
        code = main(
            [
                "onboard",
                "orchestrate",
                "plan",
                str(self.root),
                "--declaration",
                str(self.declaration),
            ],
            stdout=output,
            stderr=errors,
        )
        self.assertEqual(code, 0, errors.getvalue())
        self.assertIn("Read-only", output.getvalue())
        self.assertNotIn("--apply", output.getvalue())

    def test_change_during_final_inventory_is_detected(self):
        real = planning.inspect_gitlink_inventory
        calls = 0

        def change(root):
            nonlocal calls
            result = real(root)
            calls += 1
            if calls == 2:
                self.declare([])
            return result

        with patch.object(planning, "inspect_gitlink_inventory", side_effect=change):
            with self.assertRaises(OrchestrationInventoryError) as caught:
                planning.plan_orchestration(self.root, self.declaration)
        self.assertEqual(caught.exception.code, "orchestration.inputs_changed")

    def test_relationship_budget_and_empty_repository_refuse(self):
        self.declare([{"consumer": "app", "provider": "lib"}] * 1025)
        self.assertEqual(self.invoke("plan")[0], 2)
        self.declare([])
        empty = self.base / "empty"
        repository(empty)
        with self.assertRaises(OrchestrationInventoryError) as caught:
            planning.plan_orchestration(empty, self.declaration)
        self.assertEqual(caught.exception.code, "orchestration.gitlinks_required")

    def test_relationships_do_not_load_child_project_authority(self):
        child = self.root / "app"
        child.mkdir()
        (child / "literate.project.json").write_text(
            "invalid child-owned authority", encoding="utf-8"
        )
        before = snapshot(self.base)
        self.assertEqual(self.invoke("plan")[0], 0)
        self.assertEqual(snapshot(self.base), before)
