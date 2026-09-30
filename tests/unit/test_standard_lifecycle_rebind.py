"""Reviewed Standard lifecycle distribution rebind contracts."""

from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.standard_lifecycle_binding import (
    InstalledFrameworkDistribution,
    InstalledFrameworkDistributionMember,
    StandardLifecycleBindingError,
)
from literate_ai.application.standard_lifecycle_rebind import (
    StandardLifecycleRebindError,
    apply_standard_lifecycle_rebind,
    plan_standard_lifecycle_rebind,
)
from literate_ai.cli import main
from literate_ai.contracts import (
    PROJECT_TEST_RUNNER_EVIDENCE_KIND,
    ProjectDefinition,
    ProjectInitializationOrigin,
    ProjectTestReceiptPolicy,
    StandardProjectLifecycleDriver,
    canonical_identity,
    load_current_standard_lifecycle_policy,
)
from literate_ai.projects import ProjectConfigurationStore

ROOT = Path(__file__).resolve().parents[2]


def distribution(
    label: str, *, version: str = "0.10.0"
) -> InstalledFrameworkDistribution:
    return InstalledFrameworkDistribution(
        "literate-ai",
        version,
        (
            InstalledFrameworkDistributionMember(
                "literate_ai/__init__.py",
                len(label),
                "sha256:" + label.encode().hex().ljust(64, "0")[:64],
            ),
        ),
    )


def origin(version: str = "0.10.0") -> ProjectInitializationOrigin:
    return ProjectInitializationOrigin(
        repository_url="https://github.com/jordanhubbard/literate-ai.git",
        git_revision="a" * 40,
        distribution_name="literate-ai",
        distribution_version=version,
    )


class StandardLifecycleRebindTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Project With Spaces"
        self.root.mkdir()
        self.policy = load_current_standard_lifecycle_policy()
        self.old_distribution = distribution("old")
        self.new_distribution = distribution("new")
        old_driver = StandardProjectLifecycleDriver(
            self.old_distribution.identity, self.policy.identity
        )
        old_receipt = ProjectTestReceiptPolicy(
            self.policy.policy_id,
            self.policy.policy_version,
            old_driver.identity,
            self.policy.required_evidence_kinds,
            self.policy.minimum_test_count,
        )
        value = json.loads((ROOT / "literate.project.json").read_text())
        value["lifecycle_driver"] = old_driver.to_dict()
        value["test_receipt_policy"] = old_receipt.to_dict()
        self.original = ProjectDefinition.from_dict(value)
        ProjectConfigurationStore(self.root).create(self.original)

    def plan(self, observed=None, selected_policy=None) -> dict[str, object]:
        chosen = self.new_distribution if observed is None else observed
        policy = self.policy if selected_policy is None else selected_policy
        return plan_standard_lifecycle_rebind(
            self.root,
            distribution_observer=lambda: chosen,
            policy_loader=lambda: policy,
            origin_observer=lambda version: origin(version),
        )

    def apply(
        self,
        plan: dict[str, object],
        *,
        observer=None,
        authorize: bool = True,
    ) -> dict[str, object]:
        selected = (lambda: self.new_distribution) if observer is None else observer
        return apply_standard_lifecycle_rebind(
            self.root,
            plan,
            authorize_rebind=authorize,
            distribution_observer=selected,
            policy_loader=lambda: self.policy,
            origin_observer=lambda version: origin(version),
        )

    def test_plan_is_read_only_and_exposes_old_new_origin_and_version(self) -> None:
        before = (self.root / "literate.project.json").read_bytes()
        plan = self.plan()

        self.assertTrue(plan["change_required"])
        self.assertEqual(
            plan["installed_authority"]["distribution"]["version"], "0.10.0"
        )
        self.assertEqual(
            plan["installed_authority"]["origin"]["git_revision"], "a" * 40
        )
        self.assertNotEqual(plan["configured_binding"], plan["proposed_binding"])
        self.assertEqual((self.root / "literate.project.json").read_bytes(), before)

    def test_apply_is_authorized_compare_and_swap_and_changes_only_owned_fields(
        self,
    ) -> None:
        plan = self.plan()
        with self.assertRaises(StandardLifecycleRebindError) as denied:
            self.apply(plan, authorize=False)
        self.assertEqual(
            denied.exception.code, "standard_rebind.authorization_required"
        )

        before = json.loads((self.root / "literate.project.json").read_text())
        result = self.apply(plan)
        after = json.loads((self.root / "literate.project.json").read_text())

        self.assertEqual(result["action"], "applied")
        self.assertEqual(result["evidence"]["test_receipt"], "stale-until-rebuild")
        changed = {key for key in before if before[key] != after[key]}
        self.assertEqual(changed, {"lifecycle_driver", "test_receipt_policy"})
        self.assertEqual(
            after["lifecycle_driver"]["framework_distribution_identity"],
            self.new_distribution.identity.to_dict(),
        )
        self.assertEqual(
            after["test_receipt_policy"]["runner_identity"],
            StandardProjectLifecycleDriver(
                self.new_distribution.identity, self.policy.identity
            ).identity.to_dict(),
        )

        with self.assertRaises(StandardLifecycleRebindError) as stale:
            self.apply(plan)
        self.assertEqual(stale.exception.code, "standard_rebind.project_changed")

    def test_same_identity_is_an_explicit_no_op(self) -> None:
        plan = self.plan(self.old_distribution)
        self.assertFalse(plan["change_required"])
        before = (self.root / "literate.project.json").read_bytes()
        result = apply_standard_lifecycle_rebind(
            self.root,
            plan,
            authorize_rebind=True,
            distribution_observer=lambda: self.old_distribution,
            policy_loader=lambda: self.policy,
            origin_observer=lambda version: origin(version),
        )
        self.assertEqual(result["action"], "no-op")
        self.assertEqual((self.root / "literate.project.json").read_bytes(), before)

    def custom_receipt(self, *, standard_runner: bool = False):
        receipt = replace(
            self.original.test_receipt_policy,
            suite_id="legacy-parity",
            suite_version="2.3.0",
            runner_identity=(
                self.original.lifecycle_driver.identity
                if standard_runner
                else canonical_identity("independent-legacy-harness")
            ),
            required_evidence_kinds=(PROJECT_TEST_RUNNER_EVIDENCE_KIND,),
            minimum_test_count=17,
        )
        store = ProjectConfigurationStore(self.root)
        snapshot = store.discover(self.root)
        store.update(
            snapshot, replace(snapshot.definition, test_receipt_policy=receipt)
        )
        return receipt

    def test_custom_harness_policy_survives_distribution_upgrade(self) -> None:
        receipt = self.custom_receipt()
        before = ProjectConfigurationStore.discover(self.root)
        plan = self.plan()
        self.assertEqual(plan["proposed_receipt_policy"], receipt.to_dict())
        self.assertEqual(plan["changed_fields"], ["lifecycle_driver"])
        result = self.apply(plan)
        after = ProjectConfigurationStore.discover(self.root)
        self.assertEqual(after.definition.test_receipt_policy, receipt)
        self.assertEqual(result["receipt_policy"], receipt.to_dict())
        self.assertEqual(
            after.definition,
            replace(
                before.definition,
                lifecycle_driver=StandardProjectLifecycleDriver(
                    self.new_distribution.identity, self.policy.identity
                ),
            ),
        )

    def test_custom_standard_derived_runner_advances_without_replacing_suite(self):
        receipt = self.custom_receipt(standard_runner=True)
        proposed = StandardProjectLifecycleDriver(
            self.new_distribution.identity, self.policy.identity
        )
        expected = replace(receipt, runner_identity=proposed.identity)
        plan = self.plan()
        self.assertEqual(plan["proposed_receipt_policy"], expected.to_dict())
        self.assertEqual(
            plan["changed_fields"], ["lifecycle_driver", "test_receipt_policy"]
        )
        self.assertEqual(self.apply(plan)["receipt_policy"], expected.to_dict())

    def test_current_custom_policy_is_a_byte_preserving_no_op(self) -> None:
        self.custom_receipt()
        plan = self.plan(self.old_distribution)
        self.assertFalse(plan["change_required"])
        self.assertEqual(plan["changed_fields"], [])
        before = (self.root / "literate.project.json").read_bytes()
        result = self.apply(plan, observer=lambda: self.old_distribution)
        self.assertEqual(result["action"], "no-op")
        self.assertEqual((self.root / "literate.project.json").read_bytes(), before)

    def test_self_consistent_plan_cannot_replace_custom_receipt_authority(self):
        self.custom_receipt()
        plan = self.plan()
        plan["proposed_receipt_policy"]["suite_id"] = self.policy.policy_id
        plan.pop("identity")
        plan["identity"] = canonical_identity(plan).uri
        before = (self.root / "literate.project.json").read_bytes()
        with self.assertRaises(StandardLifecycleRebindError) as rejected:
            self.apply(plan)
        self.assertEqual(rejected.exception.code, "standard_rebind.plan_invalid")
        self.assertEqual((self.root / "literate.project.json").read_bytes(), before)

    def test_changed_custom_policy_invalidates_reviewed_plan(self) -> None:
        self.custom_receipt()
        plan = self.plan()
        store = ProjectConfigurationStore(self.root)
        snapshot = store.discover(self.root)
        store.update(
            snapshot,
            replace(
                snapshot.definition,
                test_receipt_policy=replace(
                    snapshot.definition.test_receipt_policy, minimum_test_count=18
                ),
            ),
        )
        before = (self.root / "literate.project.json").read_bytes()
        with self.assertRaises(StandardLifecycleRebindError) as rejected:
            self.apply(plan)
        self.assertEqual(rejected.exception.code, "standard_rebind.project_changed")
        self.assertEqual((self.root / "literate.project.json").read_bytes(), before)

    def test_rehashed_change_summary_cannot_hide_a_binding_update(self) -> None:
        self.custom_receipt()
        before = (self.root / "literate.project.json").read_bytes()
        for key, value in (("changed_fields", []), ("change_required", False)):
            with self.subTest(key=key):
                plan = self.plan()
                plan[key] = value
                plan.pop("identity")
                plan["identity"] = canonical_identity(plan).uri
                with self.assertRaises(StandardLifecycleRebindError) as rejected:
                    self.apply(plan)
                self.assertEqual(
                    rejected.exception.code, "standard_rebind.plan_invalid"
                )
                self.assertEqual(
                    (self.root / "literate.project.json").read_bytes(), before
                )

    def test_public_cli_plan_and_apply_preserve_custom_harness(self) -> None:
        receipt = self.custom_receipt()
        plan_path = self.root / "rebind.json"
        arguments = ["project", "lifecycle", "rebind-standard"]
        with (
            patch(
                "literate_ai.cli.project.observe_installed_framework_distribution",
                return_value=self.new_distribution,
            ),
            patch(
                "literate_ai.cli.project.observe_installed_framework_origin",
                side_effect=origin,
            ),
        ):
            output, errors = io.StringIO(), io.StringIO()
            status = main(
                [*arguments, "--project", str(self.root), "--output", str(plan_path)],
                stdout=output,
                stderr=errors,
            )
            self.assertEqual(status, 0, errors.getvalue())
            plan = json.loads(plan_path.read_bytes())
            self.assertEqual(plan["proposed_receipt_policy"], receipt.to_dict())
            self.assertEqual(plan["changed_fields"], ["lifecycle_driver"])
            output, errors = io.StringIO(), io.StringIO()
            status = main(
                [
                    *arguments,
                    str(plan_path),
                    "--project",
                    str(self.root),
                    "--apply",
                    "--authorize-rebind",
                ],
                stdout=output,
                stderr=errors,
            )
            self.assertEqual(status, 0, errors.getvalue())
            self.assertEqual(
                json.loads(output.getvalue())["result"]["receipt_policy"],
                receipt.to_dict(),
            )
        self.assertEqual(
            ProjectConfigurationStore.discover(
                self.root
            ).definition.test_receipt_policy,
            receipt,
        )

    def test_editable_unavailable_and_changed_installs_fail_closed(self) -> None:
        def editable():
            raise StandardLifecycleBindingError(
                "standard_binding.distribution_editable", "editable"
            )

        with self.assertRaises(StandardLifecycleRebindError) as rejected:
            plan_standard_lifecycle_rebind(
                self.root,
                distribution_observer=editable,
                policy_loader=lambda: self.policy,
                origin_observer=lambda version: origin(version),
            )
        self.assertEqual(
            rejected.exception.code, "standard_binding.distribution_editable"
        )

        plan = self.plan()
        with self.assertRaises(StandardLifecycleRebindError) as changed:
            self.apply(plan, observer=lambda: distribution("later"))
        self.assertEqual(
            changed.exception.code, "standard_rebind.installed_authority_changed"
        )

    def test_tampered_plan_and_changed_policy_are_rejected(self) -> None:
        plan = self.plan()
        tampered = copy.deepcopy(plan)
        tampered["installed_authority"]["distribution"]["payload_bytes"] += 1
        with self.assertRaises(StandardLifecycleRebindError) as invalid:
            self.apply(tampered)
        self.assertEqual(invalid.exception.code, "standard_rebind.plan_invalid")

        changed_policy = replace(self.policy, policy_version="9.9.9")
        with self.assertRaises(StandardLifecycleRebindError) as changed:
            apply_standard_lifecycle_rebind(
                self.root,
                plan,
                authorize_rebind=True,
                distribution_observer=lambda: self.new_distribution,
                policy_loader=lambda: changed_policy,
                origin_observer=lambda version: origin(version),
            )
        self.assertEqual(
            changed.exception.code, "standard_rebind.installed_authority_changed"
        )

    def test_post_write_distribution_drift_rolls_back_original_bytes(self) -> None:
        plan = self.plan()
        before = (self.root / "literate.project.json").read_bytes()
        observations = iter((self.new_distribution, distribution("drift")))
        with self.assertRaises(StandardLifecycleRebindError) as changed:
            self.apply(plan, observer=lambda: next(observations))
        self.assertEqual(
            changed.exception.code, "standard_rebind.installed_authority_changed"
        )
        self.assertEqual((self.root / "literate.project.json").read_bytes(), before)

    def test_nested_project_discovery_is_platform_path_independent(self) -> None:
        nested = self.root / "nested" / "source"
        nested.mkdir(parents=True)
        plan = plan_standard_lifecycle_rebind(
            nested,
            distribution_observer=lambda: self.new_distribution,
            policy_loader=lambda: self.policy,
            origin_observer=lambda version: origin(version),
        )
        self.assertTrue(plan["change_required"])


if __name__ == "__main__":
    unittest.main()
