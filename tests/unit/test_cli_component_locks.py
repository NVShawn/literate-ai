"""Public litai lock command tests."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.component_lock_operations import (
    PreparedComponentLock as _PreparedComponentLock,
)
from literate_ai.adapters.component_lock_operations import (
    operate_component_lock as _one_component_lock,
)
from literate_ai.adapters.component_lock_planning import ComponentLockPlanningError
from literate_ai.adapters.component_locks import (
    ComponentLockStore,
    ComponentLockStoreError,
)
from literate_ai.adapters.component_resolution_audits import (
    ComponentResolutionAuditStore,
)
from literate_ai.adapters.repository_lineage import FilesystemRepositoryLineageStore
from literate_ai.application.component_lock_resolution import ComponentLockResolver
from literate_ai.cli import main
from literate_ai.cli.component_locks import component_lock_from_args
from literate_ai.cli.errors import CliFailure
from literate_ai.contracts import RepositoryLineage, RepositoryParentSelection
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from tests.unit.test_component_lock_planning import _fixture
from tests.unit.test_component_lock_resolution import resolution_plan
from tests.unit.test_repository_orchestration import snapshot


def _run(arguments: list[str]) -> tuple[int, dict[str, object], str]:
    output = io.StringIO()
    errors = io.StringIO()
    status = main(arguments, stdout=output, stderr=errors)
    document = json.loads(output.getvalue() or errors.getvalue())
    return status, document, errors.getvalue()


class ComponentLockCliTests(unittest.TestCase):
    def test_fresh_component_check_and_diff_do_not_create_a_writing_mutex(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            component, flavors = _fixture(root)
            before = snapshot(root)
            with patch.object(
                ComponentLockStore,
                "operation",
                side_effect=AssertionError("read-only path acquired writing mutex"),
            ):
                for difference in (False, True):
                    report, status = component_lock_from_args(
                        SimpleNamespace(
                            component=str(component),
                            target="macos-host",
                            flavor_root=[str(flavors)],
                            flavor=["+macos", "+python"],
                            check=not difference,
                            diff=difference,
                        )
                    )
                    self.assertEqual(status, 1, report)
                    self.assertEqual(report["lock"]["state"], "missing")
            self.assertEqual(snapshot(root), before)

    def test_persisted_component_flavors_apply_to_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            component, flavors = _fixture(root)
            (root / "components").mkdir()
            component.rename(root / "components" / component.name)
            component = root / "components" / component.name
            (root / "docs").mkdir()
            (root / "literate.project.json").write_text(
                json.dumps(
                    {
                        "schema": "urn:literate-ai:schema:v2:project-definition",
                        "project_id": "component-flavor-test",
                        "version": "1.0.0",
                        "profile": "canonical",
                        "agent_skill": "SKILL.md",
                        "component_roots": ["components"],
                        "flavor_roots": ["flavors"],
                        "skill_roots": ["skills"],
                        "workflow_roots": ["workflows"],
                        "routing_roots": ["routing"],
                        "documentation_roots": ["docs"],
                        "source_intelligence": {
                            "schema": (
                                "urn:literate-ai:schema:v1:"
                                "project-source-intelligence-policy"
                            ),
                            "provider_id": "none",
                            "command": None,
                            "minimum_version": None,
                            "artifact_path": None,
                            "stages": {
                                "project-maintenance": "off",
                                "source-generation": "off",
                                "cache-consumption": "off",
                                "source-to-specification": "off",
                                "repository-source-admission": "off",
                                "structural-review": "off",
                            },
                            "artifact_publication": "metadata-only",
                        },
                        "component_flavor_selectors": {
                            "components/greeting": ["+macos", "+python"]
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            parent = RepositoryParentSelection.root()
            FilesystemRepositoryLineageStore(root).replace(
                parent, RepositoryLineage(parent, (), ())
            )
            common = [
                "lock",
                str(component),
                "--target",
                "macos-host",
                "--flavor-root",
                str(flavors),
            ]

            status, updated, errors = _run(common)
            self.assertEqual((status, errors), (0, ""))
            self.assertTrue(updated["result"]["lock_updated"])
            status, checked, errors = _run([*common, "--check"])
            self.assertEqual((status, errors), (0, ""))
            self.assertTrue(checked["result"]["current"])

    def test_update_check_diff_and_catalog_audit_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            common = [
                "lock",
                str(component),
                "--target",
                "macos-host",
                "--flavor-root",
                str(flavors),
                "--flavor=+macos",
                "--flavor=+python",
            ]
            status, updated, errors = _run(common)
            self.assertEqual((status, errors), (0, ""))
            self.assertTrue(updated["result"]["lock_updated"])
            self.assertTrue(updated["result"]["catalog_audit_updated"])
            first_lock = (component / "component.lock.json").read_bytes()

            status, repeated, _ = _run(common)
            self.assertEqual(status, 0)
            self.assertFalse(repeated["result"]["lock_updated"])
            self.assertFalse(repeated["result"]["catalog_audit_updated"])
            self.assertEqual(
                (component / "component.lock.json").read_bytes(), first_lock
            )

            status, checked, _ = _run([*common, "--check"])
            self.assertEqual(status, 0)
            self.assertTrue(checked["result"]["current"])

            specification = component / "specs" / "spec.md"
            specification.write_text(
                "# Greeting\n\n### Requirement: Changed greeting\n\n"
                "The application SHALL print a changed greeting.\n\n"
                "#### Scenario: Run changed greeting\n\n"
                "- **WHEN** the application runs\n"
                "- **THEN** it prints `hello from changed spec`\n",
                encoding="utf-8",
                newline="\n",
            )
            before = (component / "component.lock.json").read_bytes()
            status, difference, errors = _run([*common, "--diff"])
            self.assertEqual((status, errors), (1, ""))
            self.assertFalse(difference["result"]["current"])
            self.assertEqual((component / "component.lock.json").read_bytes(), before)
            paths = {
                item["path"] for item in difference["result"]["lock"]["differences"]
            }
            self.assertIn("/nodes/0/revision/specifications/0/identity/digest", paths)

    def test_check_is_nonmutating_when_lock_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            status, result, errors = _run(
                [
                    "lock",
                    str(component),
                    "--target",
                    "macos-host",
                    "--flavor-root",
                    str(flavors),
                    "--flavor=+macos",
                    "--flavor=+python",
                    "--check",
                ]
            )
            self.assertEqual((status, errors), (1, ""))
            self.assertEqual(result["result"]["lock"]["state"], "missing")
            self.assertFalse((component / "component.lock.json").exists())
            self.assertFalse(tuple(component.glob("component.resolution-audit.*.json")))

    def test_project_mode_preplans_every_component_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            plan = resolution_plan()
            failure = ComponentLockPlanningError(
                "component_lock.preflight_failed", "second Component is invalid"
            )
            args = SimpleNamespace(
                component=str(root),
                target="host",
                flavor_root=[],
                flavor=[],
                check=False,
                diff=False,
            )

            with (
                patch(
                    "literate_ai.adapters.component_lock_commands._component_roots",
                    return_value=(first, second),
                ),
                patch(
                    "literate_ai.adapters.component_lock_commands.FilesystemComponentLockPlanner.plan",
                    side_effect=(plan, failure),
                ),
                self.assertRaisesRegex(CliFailure, "second Component is invalid"),
            ):
                component_lock_from_args(args)

            for component in (first, second):
                self.assertFalse((component / "component.lock.json").exists())
                self.assertFalse(
                    tuple(component.glob("component.resolution-audit.*.json"))
                )
                self.assertFalse(
                    (component / ".component.operation.write.lock").exists()
                )

    def test_pair_publication_writes_audit_before_lock_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            plan = resolution_plan()
            result = ComponentLockResolver().resolve(
                plan, expected_input_evidence_identity=plan.identity
            )
            prepared = _PreparedComponentLock(root, plan, result)

            class Planner:
                def plan(self, *args, **kwargs):
                    return plan

            events: list[str] = []
            real_audit_update = ComponentResolutionAuditStore.update
            real_lock_update = ComponentLockStore.update

            def audit_update(store, *args, **kwargs):
                events.append("audit")
                return real_audit_update(store, *args, **kwargs)

            def lock_update(store, *args, **kwargs):
                events.append("lock")
                return real_lock_update(store, *args, **kwargs)

            with (
                patch.object(ComponentResolutionAuditStore, "update", audit_update),
                patch.object(ComponentLockStore, "update", lock_update),
            ):
                report, status = _one_component_lock(
                    prepared,
                    target="host",
                    flavors=(),
                    flavor_roots=(),
                    planner=Planner(),
                    check=False,
                    difference=False,
                )
            self.assertEqual(status, 0)
            self.assertEqual(events, ["audit", "lock"])
            self.assertTrue(report["lock_updated"])
            self.assertTrue(report["catalog_audit_updated"])

            lock_path = root / "component.lock.json"
            audit_path = root / "component.resolution-audit.host.json"
            lock_path.unlink()
            audit_path.unlink()
            with (
                patch.object(ComponentResolutionAuditStore, "update", audit_update),
                patch.object(
                    ComponentLockStore,
                    "update",
                    side_effect=ComponentLockStoreError(
                        "component_lock.injected_failure", "injected lock failure"
                    ),
                ),
                self.assertRaisesRegex(
                    ComponentLockStoreError, "injected lock failure"
                ),
            ):
                _one_component_lock(
                    prepared,
                    target="host",
                    flavors=(),
                    flavor_roots=(),
                    planner=Planner(),
                    check=False,
                    difference=False,
                )
            self.assertFalse(lock_path.exists())
            self.assertFalse(audit_path.exists())

    def test_check_and_noop_update_revalidate_after_artifact_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            plan = resolution_plan()
            result = ComponentLockResolver().resolve(
                plan, expected_input_evidence_identity=plan.identity
            )
            prepared = _PreparedComponentLock(root, plan, result)

            class StablePlanner:
                def plan(self, *args, **kwargs):
                    return plan

            _one_component_lock(
                prepared,
                target="host",
                flavors=(),
                flavor_roots=(),
                planner=StablePlanner(),
                check=False,
                difference=False,
            )
            changed = replace(
                plan, catalog_identity=canonical_identity({"catalog": "changed"})
            )

            class DriftPlanner:
                def __init__(self, stable_calls: int) -> None:
                    self.stable_calls = stable_calls
                    self.calls = 0

                def plan(self, *args, **kwargs):
                    self.calls += 1
                    return plan if self.calls <= self.stable_calls else changed

            with self.assertRaisesRegex(ComponentLockPlanningError, "inputs changed"):
                _one_component_lock(
                    prepared,
                    target="host",
                    flavors=(),
                    flavor_roots=(),
                    planner=DriftPlanner(2),
                    check=True,
                    difference=False,
                )
            lock_before = (root / "component.lock.json").read_bytes()
            audit_before = (root / "component.resolution-audit.host.json").read_bytes()
            with self.assertRaisesRegex(ComponentLockPlanningError, "inputs changed"):
                _one_component_lock(
                    prepared,
                    target="host",
                    flavors=(),
                    flavor_roots=(),
                    planner=DriftPlanner(4),
                    check=False,
                    difference=False,
                )
            self.assertEqual((root / "component.lock.json").read_bytes(), lock_before)
            self.assertEqual(
                (root / "component.resolution-audit.host.json").read_bytes(),
                audit_before,
            )

    def test_large_review_pages_and_atomically_applies_one_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            component, flavors = _fixture(root)
            (root / "components").mkdir()
            component.rename(root / "components" / component.name)
            component = root / "components" / component.name
            (root / "docs").mkdir()
            (root / "literate.project.json").write_text(
                json.dumps(
                    {
                        "schema": "urn:literate-ai:schema:v2:project-definition",
                        "project_id": "large-lock-review-test",
                        "version": "1.0.0",
                        "profile": "canonical",
                        "agent_skill": "SKILL.md",
                        "component_roots": ["components"],
                        "flavor_roots": ["flavors"],
                        "skill_roots": ["skills"],
                        "workflow_roots": ["workflows"],
                        "routing_roots": ["routing"],
                        "documentation_roots": ["docs"],
                        "source_intelligence": {
                            "schema": (
                                "urn:literate-ai:schema:v1:"
                                "project-source-intelligence-policy"
                            ),
                            "provider_id": "none",
                            "command": None,
                            "minimum_version": None,
                            "artifact_path": None,
                            "stages": {
                                "project-maintenance": "off",
                                "source-generation": "off",
                                "cache-consumption": "off",
                                "source-to-specification": "off",
                                "repository-source-admission": "off",
                                "structural-review": "off",
                            },
                            "artifact_publication": "metadata-only",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            parent = RepositoryParentSelection.root()
            FilesystemRepositoryLineageStore(root).replace(
                parent,
                RepositoryLineage(parent, (), ()),
            )
            common = [
                "lock",
                str(component),
                "--target",
                "macos-host",
                "--flavor-root",
                str(flavors),
                "--flavor=+macos",
                "--flavor=+python",
            ]
            status, _updated, errors = _run(common)
            self.assertEqual((status, errors), (0, ""))
            lock_path = component / "component.lock.json"
            stale = json.loads(lock_path.read_bytes())
            stale.update(
                {f"legacy-{index:04d}": {"retired": True} for index in range(513)}
            )
            lock_path.write_bytes(canonical_json_bytes(stale) + b"\n")
            before = lock_path.read_bytes()
            distribution = SimpleNamespace(
                identity=canonical_identity({"distribution": "test"})
            )
            with (
                patch.dict(
                    os.environ,
                    {"OBJ_DIR": str(root / "_build")},
                ),
                patch(
                    "literate_ai.adapters.component_lock_commands."
                    "observe_installed_framework_distribution",
                    return_value=distribution,
                ),
            ):
                status, started, errors = _run([*common, "--large-review", "start"])
                self.assertEqual((status, errors), (0, ""))
                result = started["result"]
                self.assertEqual(
                    result["schema"],
                    "literate-ai/component-lock-review-command@1",
                )
                transaction = result["transaction_identity"]
                first = result["next_page"]
                self.assertEqual(
                    first["schema"],
                    "literate-ai/component-lock-review-page@1",
                )
                self.assertEqual(len(first["differences"]), 512)
                self.assertEqual(
                    (component / "component.lock.json").read_bytes(),
                    before,
                )
                status, duplicate, errors = _run([*common, "--large-review", "start"])
                self.assertEqual((status, errors), (0, ""))
                self.assertEqual(
                    duplicate["result"]["transaction_identity"],
                    transaction,
                )
                status, resumed, errors = _run(
                    [
                        *common,
                        "--large-review",
                        "status",
                        "--transaction-id",
                        transaction,
                    ]
                )
                self.assertEqual((status, errors), (0, ""))
                self.assertEqual(resumed["result"]["next_page"], first)
                status, cleaned, errors = _run(
                    [
                        *common,
                        "--large-review",
                        "cleanup",
                        "--transaction-id",
                        transaction,
                    ]
                )
                self.assertEqual((status, errors), (0, ""))
                self.assertTrue(cleaned["result"]["removed"])
                status, restarted, errors = _run([*common, "--large-review", "start"])
                self.assertEqual((status, errors), (0, ""))
                self.assertEqual(
                    restarted["result"]["transaction_identity"],
                    transaction,
                )
                status, premature, errors = _run(
                    [
                        *common,
                        "--large-review",
                        "apply",
                        "--transaction-id",
                        transaction,
                    ]
                )
                self.assertEqual(status, 2)
                self.assertEqual(
                    premature["error"]["code"],
                    "component_lock.review_incomplete",
                )
                self.assertTrue(errors)
                status, wrong, errors = _run(
                    [
                        *common,
                        "--large-review",
                        "acknowledge",
                        "--transaction-id",
                        transaction,
                        "--page-identity",
                        "sha256:" + "0" * 64,
                    ]
                )
                self.assertEqual(status, 2)
                self.assertEqual(
                    wrong["error"]["code"],
                    "component_lock.review_acknowledgement_mismatch",
                )
                self.assertTrue(errors)

                status, continued, errors = _run(
                    [
                        *common,
                        "--large-review",
                        "acknowledge",
                        "--transaction-id",
                        transaction,
                        "--page-identity",
                        first["page_identity"],
                    ]
                )
                self.assertEqual((status, errors), (0, ""))
                second = continued["result"]["next_page"]
                self.assertEqual(len(second["differences"]), 1)
                status, replayed, errors = _run(
                    [
                        *common,
                        "--large-review",
                        "acknowledge",
                        "--transaction-id",
                        transaction,
                        "--page-identity",
                        first["page_identity"],
                    ]
                )
                self.assertEqual(status, 2)
                self.assertEqual(
                    replayed["error"]["code"],
                    "component_lock.review_acknowledgement_replayed",
                )
                self.assertTrue(errors)
                status, ready, errors = _run(
                    [
                        *common,
                        "--large-review",
                        "acknowledge",
                        "--transaction-id",
                        transaction,
                        "--page-identity",
                        second["page_identity"],
                    ]
                )
                self.assertEqual((status, errors), (0, ""))
                self.assertTrue(ready["result"]["ready"])

                drifted = json.loads(before)
                drifted["concurrent-change"] = True
                lock_path.write_bytes(canonical_json_bytes(drifted) + b"\n")
                status, changed, errors = _run(
                    [
                        *common,
                        "--large-review",
                        "apply",
                        "--transaction-id",
                        transaction,
                    ]
                )
                self.assertEqual(status, 2)
                self.assertEqual(
                    changed["error"]["code"],
                    "component_lock.review_transaction_mismatch",
                )
                self.assertTrue(errors)
                lock_path.write_bytes(before)
                status, applied, errors = _run(
                    [
                        *common,
                        "--large-review",
                        "apply",
                        "--transaction-id",
                        transaction,
                    ]
                )
                self.assertEqual((status, errors), (0, ""))
                self.assertTrue(applied["result"]["lock_updated"])
                self.assertFalse(applied["result"]["catalog_audit_updated"])
                self.assertTrue(applied["result"]["transaction_removed"])
                self.assertNotEqual(
                    (component / "component.lock.json").read_bytes(),
                    before,
                )

    def test_ide_shell_shaped_review_completes_three_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            component, flavors = _fixture(root)
            (root / "components").mkdir()
            component.rename(root / "components" / component.name)
            component = root / "components" / component.name
            (root / "docs").mkdir()
            (root / "literate.project.json").write_text(
                json.dumps(
                    {
                        "schema": "urn:literate-ai:schema:v2:project-definition",
                        "project_id": "ide-shell-lock-review-test",
                        "version": "1.0.0",
                        "profile": "canonical",
                        "agent_skill": "SKILL.md",
                        "component_roots": ["components"],
                        "flavor_roots": ["flavors"],
                        "skill_roots": ["skills"],
                        "workflow_roots": ["workflows"],
                        "routing_roots": ["routing"],
                        "documentation_roots": ["docs"],
                        "source_intelligence": {
                            "schema": (
                                "urn:literate-ai:schema:v1:"
                                "project-source-intelligence-policy"
                            ),
                            "provider_id": "none",
                            "command": None,
                            "minimum_version": None,
                            "artifact_path": None,
                            "stages": {
                                "project-maintenance": "off",
                                "source-generation": "off",
                                "cache-consumption": "off",
                                "source-to-specification": "off",
                                "repository-source-admission": "off",
                                "structural-review": "off",
                            },
                            "artifact_publication": "metadata-only",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            parent = RepositoryParentSelection.root()
            FilesystemRepositoryLineageStore(root).replace(
                parent,
                RepositoryLineage(parent, (), ()),
            )
            common = [
                "lock",
                str(component),
                "--target",
                "macos-host",
                "--flavor-root",
                str(flavors),
                "--flavor=+macos",
                "--flavor=+python",
            ]
            status, _updated, errors = _run(common)
            self.assertEqual((status, errors), (0, ""))
            lock_path = component / "component.lock.json"
            stale = json.loads(lock_path.read_bytes())
            node = stale["nodes"][0]
            for index in range(1025):
                node[f"plugin-{index:04d}"] = {
                    "enabled": True,
                    "views": {
                        "editor": f"view-{index:04d}",
                        "outline": f"outline-{index:04d}",
                    },
                    "commands": {
                        "open": f"file.open.{index:04d}",
                        "save": f"file.save.{index:04d}",
                    },
                }
            lock_path.write_bytes(canonical_json_bytes(stale) + b"\n")
            before = lock_path.read_bytes()
            distribution = SimpleNamespace(
                identity=canonical_identity({"distribution": "test"})
            )
            with (
                patch.dict(
                    os.environ,
                    {"OBJ_DIR": str(root / "_build")},
                ),
                patch(
                    "literate_ai.adapters.component_lock_commands."
                    "observe_installed_framework_distribution",
                    return_value=distribution,
                ),
            ):
                status, started, errors = _run([*common, "--large-review", "start"])
                self.assertEqual((status, errors), (0, ""))
                transaction = started["result"]["transaction_identity"]
                first = started["result"]["next_page"]
                self.assertEqual(len(first["differences"]), 512)
                self.assertTrue(
                    all(
                        item["path"].startswith("/nodes/0/plugin-")
                        for item in first["differences"]
                    )
                )
                page = first
                for expected_remaining in (512, 1):
                    status, continued, errors = _run(
                        [
                            *common,
                            "--large-review",
                            "acknowledge",
                            "--transaction-id",
                            transaction,
                            "--page-identity",
                            page["page_identity"],
                        ]
                    )
                    self.assertEqual((status, errors), (0, ""))
                    page = continued["result"]["next_page"]
                    self.assertEqual(len(page["differences"]), expected_remaining)
                status, ready, errors = _run(
                    [
                        *common,
                        "--large-review",
                        "acknowledge",
                        "--transaction-id",
                        transaction,
                        "--page-identity",
                        page["page_identity"],
                    ]
                )
                self.assertEqual((status, errors), (0, ""))
                self.assertTrue(ready["result"]["ready"])
                status, applied, errors = _run(
                    [
                        *common,
                        "--large-review",
                        "apply",
                        "--transaction-id",
                        transaction,
                    ]
                )
                self.assertEqual((status, errors), (0, ""))
                self.assertTrue(applied["result"]["lock_updated"])
                self.assertTrue(applied["result"]["transaction_removed"])
                self.assertNotEqual(lock_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
