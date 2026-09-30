"""Reviewed scope changes must earn new receipts without rewriting history."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters import retained_scope_refresh as refresh
from literate_ai.adapters.lifecycle_lock import project_lifecycle_lock
from tests.unit.test_retained_harness_receipts import (
    _adapter,
    _invoke,
    _legacy_project,
    _selectors,
)


class RetainedScopeRefreshTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "project"
        _legacy_project(self.root)
        _adapter().initialize(
            self.root,
            flavor_selectors=_selectors(self.root),
            source_intelligence_provider="none",
            convert=True,
        )
        self.source = self.root / "components/legacy-project-wrapper/implementation"

    def invoke(self, *arguments):
        return _invoke(
            "project",
            "retained-scope",
            "refresh",
            "--project",
            str(self.root),
            *arguments,
        )

    def plan(self):
        code, result = self.invoke()
        self.assertEqual(code, 0, result)
        return result["result"]

    def apply(self, plan):
        return self.invoke(
            "--apply",
            "--acknowledge",
            "--expected-plan-identity",
            plan["plan_identity"],
        )

    def snapshot(self):
        return {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and ".litai-locks" not in path.parts
        }

    def receipt(self, name):
        candidate = self.base / f"{name}.json"
        code, result = _invoke(
            "project",
            "test-receipt",
            "run-retained",
            str(candidate),
            "--project",
            str(self.root),
        )
        self.assertEqual(code, 0, result)
        code, result = _invoke(
            "project",
            "test-receipt",
            "update",
            str(candidate),
            "--project",
            str(self.root),
        )
        self.assertEqual(code, 0, result)

    def test_refresh_is_read_only_until_acknowledged_and_invalidates_old_receipt(self):
        self.receipt("original")
        history_names = (
            "legacy-harness-baseline.json",
            "legacy-wrapper-parity.json",
            "legacy-lift-shift.json",
        )
        history = {
            name: (self.root / ".literate" / name).read_bytes()
            for name in history_names
        }
        (self.source / "new.txt").write_text("new tracked-style source\n")
        makefile = self.source / "Makefile"
        makefile.write_text(
            makefile.read_text().replace("test:\n", "test:\n\t@test -f new.txt\n")
        )
        before = self.snapshot()
        plan = self.plan()
        self.assertEqual(plan["added"], ["new.txt"])
        self.assertEqual(plan["removed"], [])
        self.assertFalse(plan["commands_changed"])
        self.assertEqual(self.snapshot(), before)
        code, result = self.invoke(
            "--apply", "--expected-plan-identity", plan["plan_identity"]
        )
        self.assertEqual(code, 2, result)
        self.assertEqual(
            result["error"]["code"], "retained_scope.acknowledgement_required"
        )
        self.assertEqual(self.snapshot(), before)
        code, result = self.apply(plan)
        self.assertEqual(code, 0, result)
        self.assertTrue(result["result"]["applied"])
        self.assertEqual(
            json.loads((self.root / result["result"]["history"]).read_bytes()), plan
        )
        for name, content in history.items():
            self.assertEqual((self.root / ".literate" / name).read_bytes(), content)
        code, stale = _invoke(
            "project", "test-receipt", "require-current", "--project", str(self.root)
        )
        self.assertEqual(code, 2, stale)
        self.receipt("refreshed")
        code, current = _invoke(
            "project", "test-receipt", "require-current", "--project", str(self.root)
        )
        self.assertEqual(code, 0, current)
        self.assertEqual(current["result"]["state"], "current")
        self.assertEqual(
            (self.source / "new.txt").read_text(), "new tracked-style source\n"
        )

    def test_noop_and_stale_source_and_metadata_plans(self):
        plan = self.plan()
        self.assertFalse(plan["changes_required"])
        before = self.snapshot()
        code, result = self.apply(plan)
        self.assertEqual(code, 0, result)
        self.assertFalse(result["result"]["applied"])
        self.assertEqual(self.snapshot(), before)
        (self.source / "app.py").write_text("print('user edit')\n")
        code, result = self.apply(plan)
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "retained_scope.plan_stale")
        plan = self.plan()
        manifest = self.root / "literate.project.json"
        manifest.write_bytes(manifest.read_bytes() + b"\n")
        code, result = self.apply(plan)
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "retained_scope.plan_stale")

    def test_new_file_alone_invalidates_a_previously_current_receipt(self):
        self.receipt("before-addition")
        (self.source / "new.txt").write_text("new source member")
        code, current = _invoke(
            "project", "test-receipt", "require-current", "--project", str(self.root)
        )
        self.assertEqual(code, 0, current)
        code, result = self.apply(self.plan())
        self.assertEqual(code, 0, result)
        code, stale = _invoke(
            "project", "test-receipt", "require-current", "--project", str(self.root)
        )
        self.assertEqual(code, 2, stale)

    def test_specification_authoritative_project_refuses_refresh(self):
        from literate_ai.contracts.operator_adoption import ConversionAuthorityStage

        store = refresh.FilesystemConversionAuthorityStore(self.root)
        for stage in (
            ConversionAuthorityStage.RETAINED,
            ConversionAuthorityStage.DRAFTED,
            ConversionAuthorityStage.QUALIFIED,
        ):
            store.advance(
                stage,
                evidence_identities=(
                    refresh.canonical_identity({"fixture": stage.value}),
                ),
            )
        before = self.snapshot()
        code, result = self.invoke()
        self.assertEqual(code, 2, result)
        self.assertEqual(
            result["error"]["code"], "retained_scope.source_not_authoritative"
        )
        self.assertEqual(self.snapshot(), before)

    def test_removed_source_is_explicit_and_build_output_is_excluded(self):
        (self.source / "app.py").unlink()
        (self.source / "build").mkdir(exist_ok=True)
        (self.source / "build/new.bin").write_bytes(b"build output")
        plan = self.plan()
        self.assertEqual(plan["removed"], ["app.py"])
        self.assertNotIn(
            "build/new.bin", plan["new_inventory"]["source_scope"]["paths"]
        )
        code, result = self.apply(plan)
        self.assertEqual(code, 0, result)

    def test_concurrent_lifecycle_refuses_apply_but_not_plan(self):
        (self.source / "new.txt").write_text("new")
        plan = self.plan()
        with project_lifecycle_lock(self.root, operation="peer"):
            self.assertEqual(self.plan(), plan)
            code, result = self.apply(plan)
            self.assertEqual(code, 2)
            self.assertEqual(result["error"]["code"], "lifecycle.project_locked")
            for command in ("run-retained", "update"):
                code, result = _invoke(
                    "project",
                    "test-receipt",
                    command,
                    str(self.base / "candidate.json"),
                    "--project",
                    str(self.root),
                )
                self.assertEqual(code, 2)
                self.assertEqual(result["error"]["code"], "lifecycle.project_locked")

    def test_symlinked_source_and_metadata_refuse_without_writes(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "value.txt").write_text("foreign")
        alias = self.source / "alias"
        try:
            alias.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        code, result = self.invoke()
        self.assertEqual(code, 2, result)
        self.assertEqual(result["error"]["code"], "retained_scope.unsafe_path")
        alias.unlink()
        metadata = self.root / ".literate"
        metadata.rename(self.base / "metadata")
        metadata.symlink_to(self.base / "metadata", target_is_directory=True)
        code, result = self.invoke()
        self.assertEqual(code, 2, result)
        self.assertEqual(result["error"]["code"], "retained_scope.unsafe_path")

    def test_failed_policy_update_rolls_back_inventory_and_preserves_source(self):
        (self.source / "new.txt").write_text("user work")
        plan = self.plan()
        before = self.snapshot()
        with mock.patch.object(
            refresh.ProjectConfigurationStore, "update", side_effect=OSError("fault")
        ):
            code, result = self.apply(plan)
        self.assertEqual(code, 2, result)
        self.assertEqual(self.snapshot(), before)

    def test_review_failure_after_marker_write_rolls_back_all_owned_metadata(self):
        (self.source / "new.txt").write_text("user work")
        plan = self.plan()
        before = self.snapshot()
        record = refresh.record_project_authority_review

        def fail_after_record(*args, **kwargs):
            record(*args, **kwargs)
            raise OSError("fault after review write")

        with mock.patch.object(
            refresh, "record_project_authority_review", side_effect=fail_after_record
        ):
            code, result = self.apply(plan)
        self.assertEqual(code, 2, result)
        self.assertEqual(self.snapshot(), before)

    def test_git_tracked_additions_are_captured_but_ignored_files_are_not(self):
        subprocess.run(["git", "init", str(self.root)], check=True, capture_output=True)
        (self.source / ".gitignore").write_text("ignored/\nbuild/\ndist/\n")
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "add",
                "components/legacy-project-wrapper/implementation",
            ],
            check=True,
            capture_output=True,
        )
        (self.source / "newdir").mkdir()
        (self.source / "newdir/source.txt").write_text("upstream addition")
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "add",
                "components/legacy-project-wrapper/implementation/newdir",
            ],
            check=True,
            capture_output=True,
        )
        (self.source / "ignored").mkdir()
        (self.source / "ignored/cache.txt").write_text("cache")
        plan = self.plan()
        self.assertIn("newdir/source.txt", plan["added"])
        self.assertNotIn("ignored/cache.txt", plan["added"])
        self.assertEqual(
            plan["new_inventory"]["source_scope"]["policy"], "git-visible@1"
        )

    def test_partial_inventory_publication_can_be_replanned_without_old_receipt_reuse(
        self,
    ):
        (self.source / "new.txt").write_text("new")
        plan = self.plan()
        # Simulate an abrupt exit between the first atomic replace and policy CAS.
        (self.root / refresh.INVENTORY).write_bytes(
            refresh.canonical_json_bytes(plan["new_inventory"]) + b"\n"
        )
        repaired_plan = self.plan()
        self.assertEqual(repaired_plan["added"], [])
        self.assertTrue(repaired_plan["changes_required"])
        code, result = self.apply(repaired_plan)
        self.assertEqual(code, 0, result)
        self.receipt("recovered")
