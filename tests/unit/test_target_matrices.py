from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.component_locks import ComponentLockStore
from literate_ai.adapters.component_resolution_audits import (
    ComponentResolutionAuditStore,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.target_matrices import (
    TargetMatrixAggregateReceipt,
    TargetMatrixCell,
    TargetMatrixCellReceipt,
    TargetMatrixDeclaration,
    TargetMatrixError,
    TargetMatrixStageEvidence,
    run_target_matrix,
    subprocess_rebuild_executor,
)


def identity(label: str):
    return canonical_identity({"label": label})


def passing_result(cell: TargetMatrixCell) -> dict[str, object]:
    return {
        "specification": cell.component,
        "target": cell.target,
        "explicit_flavor_selectors": list(cell.flavors),
        "validated_project_authority_identity": identity("authority").uri,
        "lifecycle_request_identity": identity(f"plan:{cell.identity.uri}").uri,
        "component_lock_identities": [identity(f"lock:{cell.identity.uri}").uri],
        "source_cache_lifecycle_identity": identity(f"source:{cell.identity.uri}").uri,
        "receipt_identity": identity(f"lifecycle:{cell.identity.uri}").uri,
    }


def passing_lock_result(cell: TargetMatrixCell) -> dict[str, object]:
    return {
        "target": cell.target,
        "component_lock_identity": identity(f"lock:{cell.identity.uri}").uri,
    }


class TargetMatrixContractTests(unittest.TestCase):
    def cells(self) -> tuple[TargetMatrixCell, TargetMatrixCell]:
        return (
            TargetMatrixCell(
                "components/renderer", "linux-x86_64", ("+lang-cpp", "+build-bazel")
            ),
            TargetMatrixCell(
                "components/renderer", "windows-x86_64", ("+lang-cpp", "+build-make")
            ),
        )

    def test_declaration_identity_is_independent_of_input_order(self) -> None:
        first, second = self.cells()
        left = TargetMatrixDeclaration.create((first, second))
        right = TargetMatrixDeclaration.create((second, first))
        self.assertEqual(left.identity, right.identity)
        self.assertEqual(left.cells, right.cells)

    def test_target_and_component_selectors_fail_closed(self) -> None:
        for component, target in (
            ("../component", "host"),
            ("/components/example", "host"),
            ("components/example", "../host"),
            ("components/example", "Host"),
        ):
            with self.subTest(component=component, target=target):
                with self.assertRaises(TargetMatrixError):
                    TargetMatrixCell(component, target)

    def test_same_component_targets_execute_concurrently(self) -> None:
        declaration = TargetMatrixDeclaration.create(self.cells())
        barrier = threading.Barrier(2)
        active = 0
        maximum = 0
        guard = threading.Lock()

        def execute(cell: TargetMatrixCell, _: Path) -> dict[str, object]:
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            barrier.wait(timeout=2)
            time.sleep(0.02)
            with guard:
                active -= 1
            return passing_result(cell)

        with tempfile.TemporaryDirectory() as temporary:
            receipt = run_target_matrix(
                declaration,
                evidence_root=Path(temporary),
                executor=execute,
                jobs=2,
            )
        self.assertEqual(maximum, 2)
        self.assertTrue(receipt.accepted)
        self.assertEqual(receipt.status, "passed")
        self.assertEqual(
            {item.lock_identity for item in receipt.receipts},
            {identity(f"lock:{cell.identity.uri}") for cell in declaration.cells},
        )
        self.assertEqual(
            {item["lock_identity"] for item in receipt.to_dict()["receipts"]},
            {identity(f"lock:{cell.identity.uri}").uri for cell in declaration.cells},
        )

    def test_executor_receives_an_existing_cell_root(self) -> None:
        declaration = TargetMatrixDeclaration.create((self.cells()[0],))

        def execute(cell: TargetMatrixCell, cell_root: Path) -> dict[str, object]:
            self.assertTrue(cell_root.is_dir())
            return passing_result(cell)

        with tempfile.TemporaryDirectory() as temporary:
            receipt = run_target_matrix(
                declaration,
                evidence_root=Path(temporary),
                executor=execute,
                jobs=1,
            )
        self.assertTrue(receipt.accepted)

    def test_subprocess_executor_uses_external_bounded_disposable_runtime(self) -> None:
        cell = self.cells()[0]
        envelope = {"ok": True, "result": passing_result(cell)}
        observed_runtime_roots: list[Path] = []
        observed_commands: list[list[str]] = []

        def run(command, **kwargs):
            observed_commands.append(command)
            if "lock" in command:
                self.assertEqual(kwargs["timeout"], 12.5)
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps(
                        {"ok": True, "result": passing_lock_result(cell)}
                    ),
                    stderr="",
                )
            runtime_root = Path(command[command.index("--runtime-root") + 1])
            observed_runtime_roots.append(runtime_root)
            self.assertFalse(runtime_root.exists())
            self.assertTrue(runtime_root.parent.is_dir())
            self.assertEqual(kwargs["timeout"], 12.5)
            return mock.Mock(
                returncode=0,
                stdout=json.dumps(envelope),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            cell_root = project / "_build" / "matrix-cell"
            cell_root.mkdir(parents=True)
            executor = subprocess_rebuild_executor(
                project=project,
                allow_host_execution=False,
                timeout_seconds=12.5,
            )
            with mock.patch("literate_ai.target_matrices.run_with_tree_kill", run):
                self.assertEqual(executor(cell, cell_root), envelope["result"])
                self.assertEqual(executor(cell, cell_root), envelope["result"])
        self.assertEqual(
            [
                "lock" if "lock" in command else "rebuild"
                for command in observed_commands
            ],
            ["lock", "rebuild", "lock", "rebuild"],
        )
        self.assertEqual(len(observed_runtime_roots), 2)
        self.assertNotEqual(*observed_runtime_roots)
        self.assertTrue(
            all(
                not str(path).startswith(str(project))
                for path in observed_runtime_roots
            )
        )

    def test_fresh_multitarget_cells_lock_before_rebuild_without_cross_cell_state(
        self,
    ) -> None:
        cells = (
            TargetMatrixCell("components/renderer", "linux", ("+os-linux",)),
            TargetMatrixCell("components/renderer", "windows", ("+os-windows",)),
            TargetMatrixCell("components/renderer", "macos", ("+os-macos",)),
        )
        declaration = TargetMatrixDeclaration.create(cells)
        calls: dict[str, list[str]] = {}
        guard = threading.Lock()

        def run(command, **kwargs):
            scoped_root = Path(kwargs["env"]["LITAI_MATRIX_CELL_ROOT"])
            key = str(scoped_root)
            with guard:
                calls.setdefault(key, []).append(
                    "lock" if "lock" in command else "rebuild"
                )
            marker = scoped_root / "test-lock.json"
            cell = next(
                item
                for item in cells
                if item.target == command[command.index("--target") + 1]
            )
            if "lock" in command:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(cell.identity.uri, encoding="utf-8")
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps(
                        {"ok": True, "result": passing_lock_result(cell)}
                    ),
                    stderr="",
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), cell.identity.uri)
            return mock.Mock(
                returncode=0,
                stdout=json.dumps({"ok": True, "result": passing_result(cell)}),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            executor = subprocess_rebuild_executor(
                project=project,
                allow_host_execution=False,
            )
            with mock.patch("literate_ai.target_matrices.run_with_tree_kill", run):
                receipt = run_target_matrix(
                    declaration,
                    evidence_root=Path(temporary) / "evidence",
                    executor=executor,
                    jobs=3,
                )

        self.assertTrue(receipt.accepted)
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            set(tuple(value) for value in calls.values()), {("lock", "rebuild")}
        )

    def test_derived_project_scale_regenerates_stale_lock_before_67_rebuilds(
        self,
    ) -> None:
        cells = tuple(
            TargetMatrixCell(
                "components/renderer",
                f"target-{index:02d}",
                (f"+flavor-{index:02d}",),
            )
            for index in range(67)
        )
        by_target = {cell.target: cell for cell in cells}
        calls: dict[str, list[str]] = {}
        guard = threading.Lock()

        def run(command, **kwargs):
            cell = by_target[command[command.index("--target") + 1]]
            scoped_root = Path(kwargs["env"]["LITAI_MATRIX_CELL_ROOT"])
            marker = scoped_root / "test-lock.json"
            phase = "lock" if "lock" in command else "rebuild"
            with guard:
                calls.setdefault(str(scoped_root), []).append(phase)
            if phase == "lock":
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(cell.identity.uri, encoding="utf-8")
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps(
                        {"ok": True, "result": passing_lock_result(cell)}
                    ),
                    stderr="",
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), cell.identity.uri)
            return mock.Mock(
                returncode=0,
                stdout=json.dumps({"ok": True, "result": passing_result(cell)}),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            evidence = root / "evidence"
            stale = (
                evidence
                / "cells"
                / cells[0].identity.digest
                / "scoped-state"
                / "test-lock.json"
            )
            stale.parent.mkdir(parents=True)
            stale.write_text("stale-selector-lock", encoding="utf-8")
            executor = subprocess_rebuild_executor(
                project=project,
                allow_host_execution=False,
            )
            with mock.patch("literate_ai.target_matrices.run_with_tree_kill", run):
                receipt = run_target_matrix(
                    TargetMatrixDeclaration.create(cells),
                    evidence_root=evidence,
                    executor=executor,
                    jobs=16,
                )

        self.assertTrue(receipt.accepted)
        self.assertEqual(len(receipt.receipts), 67)
        self.assertEqual(len(calls), 67)
        self.assertEqual(
            set(tuple(value) for value in calls.values()), {("lock", "rebuild")}
        )
        self.assertNotIn(
            "component_lock.missing",
            " ".join(item.error or "" for item in receipt.receipts),
        )

    def test_selector_mismatched_or_unconsumed_scoped_lock_fails_closed(self) -> None:
        cell = self.cells()[0]

        def wrong_target(command, **_kwargs):
            self.assertIn("lock", command)
            result = passing_lock_result(cell)
            result["target"] = "windows-x86_64"
            return mock.Mock(
                returncode=0,
                stdout=json.dumps({"ok": True, "result": result}),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            cell_root = Path(temporary) / "cell"
            cell_root.mkdir()
            executor = subprocess_rebuild_executor(
                project=project,
                allow_host_execution=False,
            )
            with mock.patch(
                "literate_ai.target_matrices.run_with_tree_kill", wrong_target
            ):
                with self.assertRaisesRegex(TargetMatrixError, "exact target"):
                    executor(cell, cell_root)

            calls = 0

            def wrong_consumption(command, **_kwargs):
                nonlocal calls
                calls += 1
                if "lock" in command:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps(
                            {"ok": True, "result": passing_lock_result(cell)}
                        ),
                        stderr="",
                    )
                result = passing_result(cell)
                result["component_lock_identities"] = [identity("foreign-lock").uri]
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps({"ok": True, "result": result}),
                    stderr="",
                )

            with mock.patch(
                "literate_ai.target_matrices.run_with_tree_kill", wrong_consumption
            ):
                with self.assertRaisesRegex(TargetMatrixError, "exact scoped lock"):
                    executor(cell, cell_root)
            self.assertEqual(calls, 2)

    def test_subprocess_executor_fails_closed_on_timeout(self) -> None:
        cell = self.cells()[0]
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            cell_root = Path(temporary) / "cell"
            cell_root.mkdir()
            executor = subprocess_rebuild_executor(
                project=project,
                allow_host_execution=False,
                timeout_seconds=1,
            )

            def run(command, **_kwargs):
                if "lock" in command:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps(
                            {"ok": True, "result": passing_lock_result(cell)}
                        ),
                        stderr="",
                    )
                raise subprocess.TimeoutExpired(["litai"], 1)

            with mock.patch(
                "literate_ai.target_matrices.run_with_tree_kill",
                run,
            ):
                with self.assertRaisesRegex(TargetMatrixError, "exceeded 1 seconds"):
                    executor(cell, cell_root)

    def test_linux_windows_mixed_result_is_unaccepted_and_persisted(self) -> None:
        declaration = TargetMatrixDeclaration.create(self.cells())

        def execute(cell: TargetMatrixCell, _: Path) -> dict[str, object]:
            if cell.target == "linux-x86_64":
                raise RuntimeError("interrupted")
            return passing_result(cell)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = run_target_matrix(
                declaration, evidence_root=root, executor=execute, jobs=2
            )
            persisted = json.loads(
                (root / "aggregate-receipt.json").read_text(encoding="utf-8")
            )
        self.assertEqual(receipt.status, "partial")
        self.assertFalse(receipt.accepted)
        self.assertFalse(persisted["accepted"])
        self.assertEqual(
            {item.status for item in receipt.receipts}, {"passed", "failed"}
        )

    def test_foreign_cell_selection_cannot_launder_stage_evidence(self) -> None:
        first, second = self.cells()
        borrowed = passing_result(second)

        with tempfile.TemporaryDirectory() as temporary:
            receipt = run_target_matrix(
                TargetMatrixDeclaration.create((first,)),
                evidence_root=Path(temporary),
                executor=lambda _cell, _root: borrowed,
                jobs=1,
            )

        self.assertEqual(receipt.status, "failed")
        self.assertFalse(receipt.accepted)
        self.assertIn("exact Component, target", receipt.receipts[0].error)

    def test_cross_cell_and_predecessor_tampering_are_rejected(self) -> None:
        first, second = self.cells()
        foreign = TargetMatrixStageEvidence(
            second.identity, "plan", identity("plan"), None, True
        )
        with self.assertRaisesRegex(TargetMatrixError, "another cell"):
            TargetMatrixCellReceipt(
                first,
                "passed",
                identity("authority"),
                identity("plan"),
                identity("lock"),
                identity("source"),
                (foreign,),
            )

        stages = []
        predecessor = None
        for stage in ("plan", "lock", "source-admission", "lifecycle"):
            item = TargetMatrixStageEvidence(
                first.identity,
                stage,
                identity(stage),
                identity("wrong") if stage == "lock" else predecessor,
                True,
            )
            stages.append(item)
            predecessor = item.identity
        with self.assertRaisesRegex(TargetMatrixError, "complete accepted"):
            TargetMatrixCellReceipt(
                first,
                "passed",
                identity("authority"),
                identity("plan"),
                identity("lock"),
                identity("source"),
                tuple(stages),
            )

    def test_aggregate_identity_binds_exact_cell_receipts(self) -> None:
        declaration = TargetMatrixDeclaration.create(self.cells())
        with tempfile.TemporaryDirectory() as temporary:
            first = run_target_matrix(
                declaration,
                evidence_root=Path(temporary) / "one",
                executor=lambda cell, _: passing_result(cell),
                jobs=2,
            )
            second = run_target_matrix(
                declaration,
                evidence_root=Path(temporary) / "two",
                executor=lambda cell, _: passing_result(cell),
                jobs=1,
            )
        self.assertEqual(first.identity, second.identity)
        self.assertEqual(
            [item.identity for item in first.receipts],
            [item.identity for item in second.receipts],
        )
        self.assertEqual(TargetMatrixAggregateReceipt.from_dict(first.to_dict()), first)
        forged = first.to_dict()
        forged["accepted"] = False
        with self.assertRaisesRegex(TargetMatrixError, "accepted flag"):
            TargetMatrixAggregateReceipt.from_dict(forged)
        changed = TargetMatrixDeclaration.create(
            (
                self.cells()[0],
                TargetMatrixCell(
                    "components/renderer",
                    "macos-arm64",
                    ("+lang-cpp", "+build-bazel"),
                ),
            )
        )
        self.assertNotEqual(declaration.identity, changed.identity)
        self.assertIn(
            self.cells()[0].identity, {cell.identity for cell in changed.cells}
        )

    def test_reuse_requires_complete_identity_and_preserves_other_cells(self) -> None:
        declaration = TargetMatrixDeclaration.create(self.cells())
        authority = identity("authority")
        calls: list[str] = []

        def execute(cell: TargetMatrixCell, _: Path) -> dict[str, object]:
            calls.append(cell.identity.uri)
            return passing_result(cell)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = run_target_matrix(
                declaration, evidence_root=root, executor=execute, jobs=2
            )
            calls.clear()
            reused = run_target_matrix(
                declaration,
                evidence_root=root,
                executor=execute,
                jobs=2,
                reuse=True,
                current_authority_identity=authority,
            )
            self.assertEqual(calls, [])
            self.assertEqual(first.identity, reused.identity)

            changed_cell = TargetMatrixCell(
                "components/renderer",
                "macos-arm64",
                ("+lang-cpp", "+build-bazel"),
            )
            changed = TargetMatrixDeclaration.create((self.cells()[0], changed_cell))
            calls.clear()
            run_target_matrix(
                changed,
                evidence_root=root,
                executor=execute,
                jobs=2,
                reuse=True,
                current_authority_identity=authority,
            )
        self.assertEqual(calls, [changed_cell.identity.uri])

    def test_aggregate_rejects_receipt_reordering(self) -> None:
        declaration = TargetMatrixDeclaration.create(self.cells())
        with tempfile.TemporaryDirectory() as temporary:
            result = run_target_matrix(
                declaration,
                evidence_root=Path(temporary),
                executor=lambda cell, _: passing_result(cell),
                jobs=2,
            )
        with self.assertRaisesRegex(TargetMatrixError, "exact declared cell order"):
            TargetMatrixAggregateReceipt(
                declaration.identity,
                tuple(cell.identity for cell in declaration.cells),
                tuple(reversed(result.receipts)),
                "passed",
            )

    def test_cell_scoped_lock_and_audit_paths_do_not_touch_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            component = root / "components" / "renderer"
            component.mkdir(parents=True)
            one = root / "matrix" / "one"
            two = root / "matrix" / "two"
            with mock.patch.dict(os.environ, {"LITAI_MATRIX_CELL_ROOT": str(one)}):
                first_lock = ComponentLockStore(component)
                first_audit = ComponentResolutionAuditStore(component, "host")
            with mock.patch.dict(os.environ, {"LITAI_MATRIX_CELL_ROOT": str(two)}):
                second_lock = ComponentLockStore(component)
                second_audit = ComponentResolutionAuditStore(component, "host")
            self.assertNotEqual(first_lock.path, second_lock.path)
            self.assertNotEqual(first_audit.path, second_audit.path)
            self.assertFalse(str(first_lock.path).startswith(str(component)))
            self.assertFalse(str(first_audit.path).startswith(str(component)))
            self.assertEqual(list(component.iterdir()), [])

    def test_evidence_root_contains_no_project_copy(self) -> None:
        declaration = TargetMatrixDeclaration.create((self.cells()[0],))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_target_matrix(
                declaration,
                evidence_root=root,
                executor=lambda cell, _: passing_result(cell),
                jobs=1,
            )
            files = {path.name for path in root.rglob("*") if path.is_file()}
        self.assertEqual(files, {"receipt.json", "aggregate-receipt.json"})


if __name__ == "__main__":
    unittest.main()
