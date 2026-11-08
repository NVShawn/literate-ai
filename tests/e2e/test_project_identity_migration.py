"""A new adopted-project ID must retain lineage and earn its own receipt."""

from __future__ import annotations

import json
import os
from unittest import mock

from literate_ai.adapters import project_identity_migration as migration
from literate_ai.adapters.conversion_authority import FilesystemConversionAuthorityStore
from literate_ai.adapters.lifecycle_lock import project_lifecycle_lock
from tests.e2e.test_retained_scope_refresh import RetainedScopeRefreshTests
from tests.support.fixtures_test_retained_harness_receipts import _invoke


class ProjectIdentityMigrationTests(RetainedScopeRefreshTests):
    # Reuse the real initialized retained harness and receipt lifecycle fixture.
    # Inherited scope-refresh tests continue exercising ordinary alpha authority.

    def renamed(self, name="beta"):
        path = self.root / "literate.project.json"
        definition = json.loads(path.read_bytes())
        old = definition["project_id"]
        definition["project_id"] = name
        path.write_text(json.dumps(definition, indent=2) + "\n")
        return old

    def migrate(self, previous, *arguments):
        return _invoke(
            "project",
            "identity",
            "migrate",
            "--project",
            str(self.root),
            "--from-project-id",
            previous,
            *arguments,
        )

    def migration_plan(self, previous):
        code, result = self.migrate(previous)
        self.assertEqual(code, 0, result)
        return result["result"]

    def migration_apply(self, previous, plan):
        return self.migrate(
            previous,
            "--apply",
            "--acknowledge",
            "--expected-plan-identity",
            plan["plan_identity"],
        )

    def test_real_rename_preserves_history_and_requires_fresh_receipt(self):
        self.receipt("alpha")
        code, result = _invoke(
            "project",
            "convert-stage",
            "advance",
            "--to",
            "retained",
            "--project",
            str(self.root),
        )
        self.assertEqual(code, 0, result)
        historical = {
            p: p.read_bytes() for p in (self.root / ".literate").glob("legacy-*.json")
        }
        old_receipt = (self.root / "verification/current.json").read_bytes()
        previous = self.renamed()
        before = self.snapshot()
        plan = self.migration_plan(previous)
        self.assertEqual(before, self.snapshot())
        self.assertTrue(plan["requalification_required"])
        code, result = self.migrate(
            previous, "--apply", "--expected-plan-identity", plan["plan_identity"]
        )
        self.assertEqual(code, 2, result)
        self.assertEqual(
            result["error"]["code"], "project_identity.acknowledgement_required"
        )
        self.assertEqual(before, self.snapshot())
        code, result = self.migration_apply(previous, plan)
        self.assertEqual(code, 0, result)
        self.assertEqual(
            (self.root / "verification/current.json").read_bytes(), old_receipt
        )
        for path, content in historical.items():
            self.assertEqual(path.read_bytes(), content)
        state = FilesystemConversionAuthorityStore(self.root).load_optional()
        self.assertEqual(state.project_id, "beta")
        self.assertEqual(state.identity_migration_identity.uri, plan["record_identity"])
        code, result = _invoke(
            "project", "test-receipt", "require-current", "--project", str(self.root)
        )
        self.assertEqual(code, 2, result)
        refreshed = self.plan()
        code, result = self.apply(refreshed)
        self.assertEqual(code, 0, result)
        self.receipt("beta")
        code, result = _invoke(
            "project", "test-receipt", "require-current", "--project", str(self.root)
        )
        self.assertEqual(code, 0, result)
        self.assertEqual(result["result"]["state"], "current")

    def test_stale_source_manifest_and_conversion_plans_refuse(self):
        previous = self.renamed()
        for change in ("source", "manifest", "conversion"):
            with self.subTest(change=change):
                before = self.snapshot()
                plan = self.migration_plan(previous)
                if change == "source":
                    path = self.source / "new.txt"
                    path.write_text("changed source")
                elif change == "manifest":
                    path = self.root / "literate.project.json"
                    value = json.loads(path.read_bytes())
                    value["version"] = "9.0.0"
                    path.write_text(json.dumps(value))
                else:
                    path = self.root / ".literate/conversion-authority.json"
                    value = json.loads(path.read_bytes())
                    value["project_id"] = "unrelated"
                    from literate_ai.contracts import canonical_json_bytes

                    path.write_bytes(canonical_json_bytes(value) + b"\n")
                changed = self.snapshot()
                code, result = self.migration_apply(previous, plan)
                self.assertEqual(code, 2, result)
                self.assertEqual(changed, self.snapshot())
                if path.relative_to(self.root) in before:
                    path.write_bytes(before[path.relative_to(self.root)])
                else:
                    path.unlink()

    def test_missing_or_modified_migration_history_refuses_state_read(self):
        previous = self.renamed()
        plan = self.migration_plan(previous)
        code, result = self.migration_apply(previous, plan)
        self.assertEqual(code, 0, result)
        path = self.root / result["result"]["history"]
        content = path.read_bytes()
        for body in (None, b"{}\n"):
            with self.subTest(body=body):
                if body is None:
                    path.unlink()
                else:
                    path.write_bytes(body)
                with self.assertRaises(RuntimeError):
                    FilesystemConversionAuthorityStore(self.root).load_optional()
                path.write_bytes(content)

    def test_history_only_interruption_can_retry_without_false_authority(self):
        previous = self.renamed()
        plan = self.migration_plan(previous)
        before_state = (self.root / ".literate/conversion-authority.json").read_bytes()
        with mock.patch.object(
            FilesystemConversionAuthorityStore,
            "_replace",
            side_effect=OSError("interrupted"),
        ):
            code, result = self.migration_apply(previous, plan)
        self.assertEqual(code, 2, result)
        self.assertEqual(
            (self.root / ".literate/conversion-authority.json").read_bytes(),
            before_state,
        )
        self.assertEqual(len(list((self.root / migration.HISTORY).glob("*.json"))), 1)
        self.assertEqual(
            self.migration_plan(previous)["plan_identity"], plan["plan_identity"]
        )
        self.assertEqual(self.migration_apply(previous, plan)[0], 0)

    def test_collision_symlink_and_concurrent_lifecycle_refuse(self):
        previous = self.renamed()
        plan = self.migration_plan(previous)
        with project_lifecycle_lock(self.root, operation="fixture"):
            code, result = self.migration_apply(previous, plan)
        self.assertEqual(code, 2, result)
        history = self.root / migration.HISTORY
        history.mkdir()
        target = self.base / "foreign"
        target.write_text("foreign")
        path = self.root / plan["write_set"][0]
        path.write_text("collision")
        self.assertEqual(self.migration_apply(previous, plan)[0], 2)
        self.assertEqual(path.read_text(), "collision")
        path.unlink()
        if hasattr(os, "symlink"):
            try:
                path.symlink_to(target)
            except OSError:
                self.skipTest("symlink fixture permission unavailable")
            self.assertEqual(self.migration_apply(previous, plan)[0], 2)
            self.assertEqual(target.read_text(), "foreign")

    def test_two_renames_keep_an_independently_validated_chain(self):
        previous = self.renamed("beta")
        self.assertEqual(
            self.migration_apply(previous, self.migration_plan(previous))[0], 0
        )
        previous = self.renamed("gamma")
        self.assertEqual(
            self.migration_apply(previous, self.migration_plan(previous))[0], 0
        )
        state = FilesystemConversionAuthorityStore(self.root).load_optional()
        self.assertEqual(state.project_id, "gamma")
        records = list((self.root / migration.HISTORY).glob("*.json"))
        self.assertEqual(len(records), 2)
        first = next(
            p for p in records if json.loads(p.read_bytes())["to_project_id"] == "beta"
        )
        first.unlink()
        with self.assertRaises(RuntimeError):
            FilesystemConversionAuthorityStore(self.root).load_optional()

    def test_closing_source_change_rolls_back_only_the_owned_state(self):
        previous = self.renamed()
        plan = self.migration_plan(previous)
        state_path = self.root / ".literate/conversion-authority.json"
        before = state_path.read_bytes()
        original = FilesystemConversionAuthorityStore._replace
        changed = self.source / "foreign-change.txt"

        def publish(store, state, *, expected):
            original(store, state, expected=expected)
            if state.project_id == "beta":
                changed.write_text("concurrent authored input")

        with mock.patch.object(FilesystemConversionAuthorityStore, "_replace", publish):
            code, result = self.migration_apply(previous, plan)
        self.assertEqual(code, 2, result)
        self.assertEqual(result["error"]["code"], "project_identity.source_changed")
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(changed.read_text(), "concurrent authored input")
        self.assertEqual(len(list((self.root / migration.HISTORY).glob("*.json"))), 1)

    def test_qualified_authority_is_not_renamed_by_the_source_operation(self):
        from dataclasses import replace

        from literate_ai.contracts import canonical_identity, canonical_json_bytes
        from literate_ai.contracts.operator_adoption import ConversionAuthorityStage

        store = FilesystemConversionAuthorityStore(self.root)
        state = store.load_optional()
        qualified = replace(
            state,
            stage=ConversionAuthorityStage.QUALIFIED,
            prior_state_identity=canonical_identity({"fixture": "not a qualification"}),
        )
        path = self.root / ".literate/conversion-authority.json"
        path.write_bytes(canonical_json_bytes(qualified.to_dict()) + b"\n")
        previous = self.renamed()
        before = self.snapshot()
        code, result = self.migrate(previous)
        self.assertEqual(code, 2, result)
        self.assertEqual(result["error"]["code"], "project_identity.stage_unsupported")
        self.assertEqual(before, self.snapshot())

    def test_missing_plan_wrong_old_id_and_unchanged_identity_refuse(self):
        old = json.loads((self.root / "literate.project.json").read_bytes())[
            "project_id"
        ]
        self.assertEqual(self.migrate(old)[0], 2)
        previous = self.renamed()
        self.assertEqual(self.migrate("unrelated")[0], 2)
        code, result = self.migrate(previous, "--apply", "--acknowledge")
        self.assertEqual(code, 2, result)
        self.assertEqual(result["error"]["code"], "project_identity.plan_required")
