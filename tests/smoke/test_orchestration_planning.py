"""Public Gitlink orchestration planning without host or repository side effects."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import orchestration_planning as planning
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
