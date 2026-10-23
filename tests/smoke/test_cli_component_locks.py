"""Public litai lock command tests."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import component_lock_operations
from literate_ai.adapters.component_lock_operations import (
    PreparedComponentLock as _PreparedComponentLock,
)
from literate_ai.adapters.component_lock_operations import (
    operate_component_lock as _one_component_lock,
)
from literate_ai.adapters.component_lock_planning import (
    ComponentLockPlanningError,
    FilesystemComponentLockPlanner,
)
from literate_ai.adapters.component_locks import (
    ComponentLockStore,
    ComponentLockStoreError,
)
from literate_ai.adapters.component_resolution_audits import (
    ComponentResolutionAuditStore,
)
from literate_ai.application.component_lock_resolution import ComponentLockResolver
from literate_ai.cli import main
from tests.support.fixtures_test_component_lock_planning import _fixture
from tests.support.fixtures_test_component_lock_resolution import resolution_plan


def _run(arguments: list[str]) -> tuple[int, dict[str, object], str]:
    output = io.StringIO()
    errors = io.StringIO()
    status = main(arguments, stdout=output, stderr=errors)
    document = json.loads(output.getvalue() or errors.getvalue())
    return status, document, errors.getvalue()


class ComponentLockCliTests(unittest.TestCase):
    def test_read_only_reuse_does_not_hide_changed_requested_flavor_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            component, flavors = _fixture(root)
            planner = FilesystemComponentLockPlanner()
            prepared = component_lock_operations.prepare_component_lock(
                component,
                target="macos-host",
                flavors=("+macos", "+python"),
                flavor_roots=(flavors,),
                planner=planner,
            )
            other = root / "other-flavors"
            shutil.copytree(flavors, other)
            changed = other / "lang-python/flavor.md"
            changed.write_bytes(changed.read_bytes() + b"\n")
            with self.assertRaises(ComponentLockPlanningError):
                _one_component_lock(
                    prepared,
                    target="macos-host",
                    flavors=("+macos", "+python"),
                    flavor_roots=(other,),
                    planner=planner,
                    check=True,
                    difference=False,
                )
            self.assertFalse((component / "component.lock.json").exists())
            self.assertFalse(tuple(component.glob("component.resolution-audit.*.json")))

    def test_read_only_check_captures_catalog_once_per_operation(self) -> None:
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
            self.assertEqual(_run(common)[0], 0)
            lock_path = component / "component.lock.json"
            original = lock_path.read_bytes()
            capture = FilesystemComponentLockPlanner.snapshot
            with patch.object(
                FilesystemComponentLockPlanner,
                "snapshot",
                autospec=True,
                side_effect=capture,
            ) as observations:
                for operation in ("--check", "--diff"):
                    status, result, errors = _run([*common, operation])
                    self.assertEqual((status, errors), (0, ""))
                    self.assertTrue(result["result"]["current"])
                self.assertEqual(observations.call_count, 2)
            self.assertEqual(lock_path.read_bytes(), original)

    def test_read_only_snapshot_reuse_refuses_in_operation_catalog_drift(self) -> None:
        for mutation in ("component", "flavor", "specification", "membership"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as temporary,
            ):
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
                self.assertEqual(_run(common)[0], 0)
                artifacts = [
                    component / "component.lock.json",
                    component / "component.resolution-audit.macos-host.json",
                ]
                original = {path: path.read_bytes() for path in artifacts}
                real_checks = component_lock_operations.component_artifact_checks
                changed = False

                def checks(
                    *args,
                    real_checks=real_checks,
                    mutation=mutation,
                    component=component,
                    flavors=flavors,
                ):
                    nonlocal changed
                    result = real_checks(*args)
                    if not changed:
                        changed = True
                        if mutation == "membership":
                            shutil.copytree(flavors / "lang-python", flavors / "added")
                        else:
                            path = {
                                "component": component / "component.md",
                                "flavor": flavors / "lang-python/flavor.md",
                                "specification": component / "specs/spec.md",
                            }[mutation]
                            path.write_bytes(path.read_bytes() + b"\n")
                    return result

                with patch.object(
                    component_lock_operations, "component_artifact_checks", checks
                ):
                    status, _, _ = _run([*common, "--check"])
                self.assertTrue(changed)
                self.assertEqual(status, 2)
                self.assertEqual(
                    {path: path.read_bytes() for path in artifacts}, original
                )

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

    def test_pair_publication_writes_audit_before_lock_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            plan = resolution_plan()
            result = ComponentLockResolver().resolve(
                plan, expected_input_evidence_identity=plan.identity
            )
            prepared = _PreparedComponentLock(root, plan, result)

            class Planner:
                def plan_with_snapshot(self, *args, **kwargs):
                    return plan, None

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


if __name__ == "__main__":
    unittest.main()
