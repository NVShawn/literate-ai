"""Registered child updates retain exact parent/child review and rollback custody."""

from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import native_project_updates as updates
from literate_ai.adapters.conversion_authority import (
    NATIVE_PROJECTS_FILE,
    ConversionAuthorityError,
    _native_project_roots,
    register_native_project,
)
from literate_ai.application.standard_lifecycle_rebind import (
    StandardLifecycleRebindError,
)
from literate_ai.cli import main
from literate_ai.contracts import (
    ProjectDefinition,
    ProjectTestReceiptPolicy,
    StandardProjectLifecycleDriver,
    load_current_standard_lifecycle_policy,
)
from literate_ai.projects import ProjectConfigurationStore, load_project
from tests.smoke.test_standard_lifecycle_rebind import distribution, origin

ROOT = Path(__file__).resolve().parents[2]


class NativeProjectUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve() / "Adopted Project"
        self.child = self.parent / ".literate/native-projects/tiny"
        self.child.mkdir(parents=True)
        policy = load_current_standard_lifecycle_policy()
        driver = StandardProjectLifecycleDriver(
            distribution("old").identity, policy.identity
        )
        value = json.loads((ROOT / "literate.project.json").read_text())
        value["lifecycle_driver"] = driver.to_dict()
        value["test_receipt_policy"] = ProjectTestReceiptPolicy(
            policy.policy_id,
            policy.policy_version,
            driver.identity,
            policy.required_evidence_kinds,
            policy.minimum_test_count,
        ).to_dict()
        definition = ProjectDefinition.from_dict(value)
        ProjectConfigurationStore(self.parent).create(
            replace(definition, project_id="adopted-parent")
        )
        ProjectConfigurationStore(self.child).create(
            replace(definition, project_id="tiny-native")
        )
        register_native_project(load_project(self.parent), self.child)
        self.registry = self.parent / NATIVE_PROJECTS_FILE
        self.manifest = self.child / "literate.project.json"
        self.observers = {
            "distribution_observer": lambda: distribution("new"),
            "origin_observer": origin,
        }

    def plan(self):
        return updates.plan_registered_standard_rebind(
            self.parent, self.child, **self.observers
        )

    def apply(self, plan, **observers):
        return updates.apply_registered_standard_rebind(
            self.parent,
            self.child,
            plan,
            authorize_rebind=True,
            **(self.observers | observers),
        )

    def test_rebind_keeps_registered_child_current_and_noop_preserves_bytes(self):
        plan = self.plan()
        old = self.registry.read_bytes()
        result = self.apply(plan)
        self.assertNotEqual(self.registry.read_bytes(), old)
        self.assertEqual(
            _native_project_roots(load_project(self.parent)), (self.child,)
        )
        self.assertEqual(result["action"], "applied")
        self.registry.write_bytes(b"\n" + self.registry.read_bytes())
        prior_registry, prior_manifest = (
            self.registry.read_bytes(),
            self.manifest.read_bytes(),
        )
        noop = self.apply(self.plan())
        self.assertEqual(noop["action"], "no-op")
        self.assertEqual(self.registry.read_bytes(), prior_registry)
        self.assertEqual(self.manifest.read_bytes(), prior_manifest)

    def test_rebind_retains_exact_prior_metadata_as_history(self):
        plan = self.plan()
        before = self.registry.read_bytes(), self.manifest.read_bytes()
        result = self.apply(plan)
        recovery = json.loads((self.parent / result["recovery"]["path"]).read_bytes())
        self.assertEqual(recovery["plan"], plan)
        self.assertEqual(
            base64.b64decode(recovery["before"]["registry_base64"]), before[0]
        )
        self.assertEqual(
            base64.b64decode(recovery["before"]["child_manifest_base64"]), before[1]
        )
        self.assertEqual(
            base64.b64decode(recovery["after"]["registry_base64"]),
            self.registry.read_bytes(),
        )
        self.assertEqual(
            base64.b64decode(recovery["after"]["child_manifest_base64"]),
            self.manifest.read_bytes(),
        )
        self.assertNotIn("recovery", self.apply(self.plan()))

    def test_existing_recovery_record_is_never_overwritten(self):
        plan = self.plan()
        before = self.registry.read_bytes(), self.manifest.read_bytes()
        record = (
            self.parent
            / ".literate/native-updates"
            / (plan["identity"][7:23] + ".json")
        )
        record.parent.mkdir()
        record.write_bytes(b"retained unrelated or conflicting history")
        with self.assertRaises(FileExistsError):
            self.apply(plan)
        self.assertEqual(
            record.read_bytes(), b"retained unrelated or conflicting history"
        )
        self.assertEqual(
            (self.registry.read_bytes(), self.manifest.read_bytes()), before
        )

    def test_indirect_recovery_directory_refuses_and_restores_both_files(self):
        plan = self.plan()
        before = self.registry.read_bytes(), self.manifest.read_bytes()
        external = self.parent.parent / "external-recovery"
        external.mkdir()
        location = self.parent / ".literate/native-updates"
        try:
            location.symlink_to(external, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(ConversionAuthorityError):
            self.apply(plan)
        self.assertEqual(list(external.iterdir()), [])
        self.assertEqual(
            (self.registry.read_bytes(), self.manifest.read_bytes()), before
        )

    def test_failed_recovery_retention_rolls_back_both_authorities(self):
        plan = self.plan()
        before = self.registry.read_bytes(), self.manifest.read_bytes()
        with (
            patch.object(
                updates,
                "_retain_rebind_metadata",
                side_effect=OSError("injected storage failure"),
            ),
            self.assertRaises(OSError),
        ):
            self.apply(plan)
        self.assertEqual(
            (self.registry.read_bytes(), self.manifest.read_bytes()), before
        )
        self.assertEqual(
            _native_project_roots(load_project(self.parent)), (self.child,)
        )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFOs")
    def test_special_registry_is_refused_without_waiting_for_a_writer(self):
        original = self.manifest.read_bytes()
        self.registry.unlink()
        os.mkfifo(self.registry)
        with self.assertRaises(StandardLifecycleRebindError) as refused:
            self.plan()
        self.assertEqual(
            refused.exception.code, "standard_rebind.registered_metadata_invalid"
        )
        self.assertEqual(self.manifest.read_bytes(), original)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFOs")
    def test_special_parent_or_child_manifest_cannot_stall_an_update(self):
        for manifest in (self.parent / "literate.project.json", self.manifest):
            with self.subTest(manifest=str(manifest.relative_to(self.parent))):
                original = manifest.read_bytes()
                registration = self.registry.read_bytes()
                manifest.unlink()
                os.mkfifo(manifest)
                try:
                    with self.assertRaises(StandardLifecycleRebindError):
                        self.plan()
                    self.assertEqual(self.registry.read_bytes(), registration)
                finally:
                    manifest.unlink()
                    manifest.write_bytes(original)

    def test_plan_tampering_and_missing_authorization_preserve_both_files(self):
        before = self.registry.read_bytes(), self.manifest.read_bytes()
        plan = self.plan()
        changed = dict(plan, registration={})
        with self.assertRaises(StandardLifecycleRebindError) as refused:
            self.apply(changed)
        self.assertEqual(refused.exception.code, "standard_rebind.plan_invalid")
        with self.assertRaises(StandardLifecycleRebindError) as refused:
            updates.apply_registered_standard_rebind(
                self.parent, self.child, plan, authorize_rebind=False, **self.observers
            )
        self.assertEqual(
            refused.exception.code, "standard_rebind.authorization_required"
        )
        self.assertEqual(
            (self.registry.read_bytes(), self.manifest.read_bytes()), before
        )

    def test_concurrent_registry_edit_is_never_overwritten_by_rollback(self):
        plan = self.plan()
        manifest = self.manifest.read_bytes()
        concurrent = b"\n" + self.registry.read_bytes()
        original = updates.apply_standard_lifecycle_rebind

        def update_then_concurrent_registry(*args, **kwargs):
            result = original(*args, **kwargs)
            self.registry.write_bytes(concurrent)
            return result

        with (
            patch.object(
                updates,
                "apply_standard_lifecycle_rebind",
                side_effect=update_then_concurrent_registry,
            ),
            self.assertRaises(StandardLifecycleRebindError) as refused,
        ):
            self.apply(plan)
        self.assertEqual(
            refused.exception.code, "standard_rebind.registered_rollback_failed"
        )
        self.assertEqual(self.registry.read_bytes(), concurrent)
        self.assertEqual(self.manifest.read_bytes(), manifest)

    def test_changed_registry_refuses_without_changing_either_input(self):
        plan = self.plan()
        self.registry.write_bytes(b"\n" + self.registry.read_bytes())
        before = self.registry.read_bytes(), self.manifest.read_bytes()
        with self.assertRaises(StandardLifecycleRebindError):
            self.apply(plan)
        self.assertEqual(
            (self.registry.read_bytes(), self.manifest.read_bytes()), before
        )

    def test_nested_child_selection_preserves_the_exact_registered_root(self):
        nested = self.child / "components"
        nested.mkdir()
        plan = updates.plan_registered_standard_rebind(
            self.parent, nested, **self.observers
        )
        updates.apply_registered_standard_rebind(
            self.parent, nested, plan, authorize_rebind=True, **self.observers
        )
        self.assertEqual(
            _native_project_roots(load_project(self.parent)), (self.child,)
        )

    def test_plan_refuses_metadata_changed_during_registry_observation(self):
        original_registry = self.registry.read_bytes()
        original_manifest = self.manifest.read_bytes()
        original = updates._native_project_roots
        for target in (self.registry, self.manifest):
            with self.subTest(target=target.name):
                self.registry.write_bytes(original_registry)
                self.manifest.write_bytes(original_manifest)

                def change_after_validation(parent, target=target):
                    roots = original(parent)
                    if target == self.registry:
                        target.write_bytes(b"\n" + original_registry)
                    else:
                        value = json.loads(original_manifest)
                        value["project_id"] = "concurrently-changed"
                        target.write_text(json.dumps(value))
                    return roots

                with (
                    patch.object(
                        updates,
                        "_native_project_roots",
                        side_effect=change_after_validation,
                    ),
                    self.assertRaises(StandardLifecycleRebindError),
                ):
                    self.plan()

    def test_changed_child_identity_is_never_silently_registered(self):
        plan = self.plan()
        value = json.loads(self.manifest.read_bytes())
        value["project_id"] = "changed-child"
        self.manifest.write_text(json.dumps(value))
        before = self.registry.read_bytes(), self.manifest.read_bytes()
        with self.assertRaises(StandardLifecycleRebindError):
            self.apply(plan)
        self.assertEqual(
            (self.registry.read_bytes(), self.manifest.read_bytes()), before
        )

    def test_registry_write_failure_restores_exact_old_child_and_registration(self):
        plan = self.plan()
        before = self.registry.read_bytes(), self.manifest.read_bytes()
        original = updates._replace_metadata

        def fail_registration(parent, path, previous, content):
            if path == self.registry:
                raise OSError("injected registry write failure")
            original(parent, path, previous, content)

        with (
            patch.object(updates, "_replace_metadata", side_effect=fail_registration),
            self.assertRaises(OSError),
        ):
            self.apply(plan)
        self.assertEqual(
            (self.registry.read_bytes(), self.manifest.read_bytes()), before
        )
        self.assertEqual(
            _native_project_roots(load_project(self.parent)), (self.child,)
        )

    def test_post_write_distribution_drift_restores_both_authorities(self):
        self.manifest.write_bytes(b"\n" + self.manifest.read_bytes())
        plan = self.plan()
        before = self.registry.read_bytes(), self.manifest.read_bytes()
        observations = iter((distribution("new"), distribution("drift")))
        with self.assertRaises(StandardLifecycleRebindError) as rejected:
            self.apply(plan, distribution_observer=lambda: next(observations))
        self.assertEqual(
            rejected.exception.code, "standard_rebind.installed_authority_changed"
        )
        self.assertEqual(
            (self.registry.read_bytes(), self.manifest.read_bytes()), before
        )

    def test_final_registry_validation_failure_restores_both_old_files(self):
        plan = self.plan()
        before = self.registry.read_bytes(), self.manifest.read_bytes()
        original = updates._native_project_roots

        def reject_updated_registry(parent):
            if self.registry.read_bytes() != before[0]:
                raise ConversionAuthorityError(
                    "conversion_authority.native_registry_invalid",
                    "injected final registry validation failure",
                )
            return original(parent)

        with (
            patch.object(
                updates, "_native_project_roots", side_effect=reject_updated_registry
            ),
            self.assertRaises(ConversionAuthorityError),
        ):
            self.apply(plan)
        self.assertEqual(
            (self.registry.read_bytes(), self.manifest.read_bytes()), before
        )

    def test_public_cli_requires_and_applies_reviewed_parent_coordination(self):
        arguments = [
            "project",
            "lifecycle",
            "rebind-standard",
            "--project",
            str(self.child),
        ]
        output, errors = io.StringIO(), io.StringIO()
        status = main(arguments, stdout=output, stderr=errors)
        self.assertEqual(status, 2)
        self.assertEqual(
            json.loads(errors.getvalue())["error"]["code"],
            "standard_rebind.registered_parent_required",
        )
        plan_path = self.parent / "registered-rebind.json"
        with (
            patch(
                "literate_ai.cli.project.observe_installed_framework_distribution",
                return_value=distribution("new"),
            ),
            patch(
                "literate_ai.cli.project.observe_installed_framework_origin",
                side_effect=origin,
            ),
        ):
            arguments += ["--registered-parent", str(self.parent)]
            for extra in (
                ["--output", str(plan_path)],
                [str(plan_path), "--apply", "--authorize-rebind"],
            ):
                output, errors = io.StringIO(), io.StringIO()
                status = main([*arguments, *extra], stdout=output, stderr=errors)
                self.assertEqual(status, 0, errors.getvalue())
        self.assertEqual(
            _native_project_roots(load_project(self.parent)), (self.child,)
        )

    def test_child_manifest_symlink_refuses_before_mutation(self):
        plan = self.plan()
        original = self.manifest.read_bytes()
        outside = self.parent.parent / "outside.json"
        outside.write_bytes(original)
        self.manifest.unlink()
        try:
            self.manifest.symlink_to(outside)
        except OSError:
            self.skipTest("host does not permit symlink creation")
        registry = self.registry.read_bytes()
        with self.assertRaises(StandardLifecycleRebindError):
            self.apply(plan)
        self.assertTrue(self.manifest.is_symlink())
        self.assertEqual(outside.read_bytes(), original)
        self.assertEqual(self.registry.read_bytes(), registry)


if __name__ == "__main__":
    unittest.main()
