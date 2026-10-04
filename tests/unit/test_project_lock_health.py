"""Detailed lock observations retain checker evidence and verify's scope/verdicts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.lock_command_errors import LockCommandError
from literate_ai.adapters.project_lock_health import observe_project_locks
from tests.support.fixtures_test_repository_orchestration import snapshot


class ProjectLockHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = SimpleNamespace(
            root=self.root,
            definition=SimpleNamespace(
                project_id="fixture",
                component_roots=("components",),
                repository_orchestration=None,
            ),
        )
        discovery = patch(
            "literate_ai.adapters.project_lock_health.discover_project",
            return_value=self.project,
        )
        discovery.start()
        self.addCleanup(discovery.stop)

    def component(self, path: str, *, locked: bool = True) -> Path:
        root = self.root / path
        root.mkdir(parents=True)
        (root / "component.md").write_text("fixture\n", encoding="utf-8")
        if locked:
            (root / "component.lock.json").write_text("{}\n", encoding="utf-8")
        return root

    def test_retains_exact_reports_and_distinguishes_same_named_components(self):
        self.component("components/a/shared")
        self.component("components/b/shared")
        self.component("components/c/broken")
        self.component("components/unlocked", locked=False)
        self.component("_build/scratch")
        reports = [
            {"current": True, "lock": {"state": "current"}},
            {
                "current": False,
                "lock": {"state": "current"},
                "catalog_audit": {"state": "stale"},
                "catalog_audit_identity": "expected-audit-identity",
                "provider_resolutions": [{"selected_provider": "fixture"}],
            },
        ]
        before = snapshot(self.root)
        with patch(
            "literate_ai.adapters.component_lock_commands.component_lock_from_args",
            side_effect=[
                (reports[0], 0),
                (reports[1], 1),
                LockCommandError("component_lock.inputs_changed", "catalog changed"),
            ],
        ) as check:
            observed = observe_project_locks(self.root)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(check.call_count, 3)
        for call in check.call_args_list:
            args = call.args[0]
            self.assertEqual(
                (args.target, args.flavor, args.flavor_root, args.check, args.diff),
                ("host", [], [], True, False),
            )
        self.assertEqual(observed.gate.state, "fail")
        self.assertEqual(
            observed.gate.detail,
            "shared: not current; broken: component_lock.inputs_changed",
        )
        self.assertEqual(
            [row["component"] for row in observed.components],
            ["components/a/shared", "components/b/shared", "components/c/broken"],
        )
        self.assertEqual(
            [row["state"] for row in observed.components],
            ["current", "not-current", "error"],
        )
        self.assertEqual(observed.components[1]["report"], reports[1])
        self.assertEqual(
            observed.components[2]["error"],
            {"code": "component_lock.inputs_changed", "message": "catalog changed"},
        )
        self.assertIsNone(observed.components[2]["report"])

    def test_all_current_preserves_rollup(self):
        self.component("components/one")
        self.component("components/two")
        with patch(
            "literate_ai.adapters.component_lock_commands.component_lock_from_args",
            return_value=({"current": True}, 0),
        ):
            observed = observe_project_locks(self.root)
        self.assertEqual(observed.gate.state, "pass")
        self.assertEqual(observed.gate.detail, "2 committed lock(s) current")
        self.assertEqual(len(observed.components), 2)

    def test_repository_failure_leaves_component_checks_unobserved(self):
        self.project.definition.repository_orchestration = object()
        self.component("components/one")
        repository = {"state": "stale", "expected_identity": "current-inputs"}
        with (
            patch(
                "literate_ai.adapters.repository_lock_commands.repository_lock_check",
                return_value=repository,
            ),
            patch(
                "literate_ai.adapters.component_lock_commands.component_lock_from_args"
            ) as check,
        ):
            observed = observe_project_locks(self.root)
        check.assert_not_called()
        self.assertEqual(observed.repository, repository)
        self.assertEqual(observed.components, ())
        self.assertEqual(observed.gate.state, "fail")
        self.assertEqual(
            observed.gate.detail,
            "repository lock is stale; run litai lock on this root",
        )

    def test_empty_scope_is_skipped_unless_repository_lock_is_current(self):
        self.component("components/unlocked", locked=False)
        observed = observe_project_locks(self.root)
        self.assertEqual(observed.gate.state, "skipped")
        self.assertEqual(observed.components, ())
        self.project.definition.repository_orchestration = object()
        with patch(
            "literate_ai.adapters.repository_lock_commands.repository_lock_check",
            return_value={"state": "current"},
        ):
            observed = observe_project_locks(self.root)
        self.assertEqual(observed.gate.state, "pass")
        self.assertEqual(
            observed.gate.detail,
            "repository lock current; no committed root Component locks",
        )

    def test_repository_refusal_preserves_diagnostic(self):
        self.project.definition.repository_orchestration = object()
        with patch(
            "literate_ai.adapters.repository_lock_commands.repository_lock_check",
            side_effect=LockCommandError("orchestration.inputs_changed", "input drift"),
        ):
            observed = observe_project_locks(self.root)
        self.assertEqual(observed.gate.state, "fail")
        self.assertEqual(
            observed.gate.detail, "orchestration.inputs_changed: input drift"
        )
        self.assertIsNone(observed.repository)
        self.assertEqual(observed.components, ())
