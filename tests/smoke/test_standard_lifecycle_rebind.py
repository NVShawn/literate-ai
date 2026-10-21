"""Reviewed Standard lifecycle distribution rebind contracts."""

from __future__ import annotations

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
)
from literate_ai.application.standard_lifecycle_rebind import (
    STANDARD_FIRST_BIND_PLAN_SCHEMA,
    STANDARD_REBIND_PLAN_SCHEMA,
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
        selected_policy=None,
    ) -> dict[str, object]:
        selected = (lambda: self.new_distribution) if observer is None else observer
        return apply_standard_lifecycle_rebind(
            self.root,
            plan,
            authorize_rebind=authorize,
            distribution_observer=selected,
            policy_loader=lambda: (
                self.policy if selected_policy is None else selected_policy
            ),
            origin_observer=lambda version: origin(version),
        )

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

    def receipt_only(self, *, standard_suite: bool = False):
        receipt = (
            self.original.test_receipt_policy
            if standard_suite
            else self.custom_receipt()
        )
        store = ProjectConfigurationStore(self.root)
        snapshot = store.discover(self.root)
        store.update(snapshot, replace(snapshot.definition, lifecycle_driver=None))
        return receipt

    @staticmethod
    def rehash(plan):
        payload = {key: value for key, value in plan.items() if key != "identity"}
        return {**payload, "identity": canonical_identity(payload).uri}

    def test_first_binding_public_cli_preserves_policy_and_history(self) -> None:
        receipt = self.receipt_only()
        historical = self.root / self.original.test_receipt
        historical.parent.mkdir(parents=True)
        history = b'{"historical-evidence":"retain without admission"}\n'
        historical.write_bytes(history)
        before = (self.root / "literate.project.json").read_bytes()
        plan_path = self.root / "first-binding.json"
        arguments = [
            "project",
            "lifecycle",
            "rebind-standard",
            "--project",
            str(self.root),
        ]
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
                [*arguments, "--output", str(plan_path)], stdout=output, stderr=errors
            )
            self.assertEqual(status, 0, errors.getvalue())
            self.assertEqual((self.root / "literate.project.json").read_bytes(), before)
            plan = json.loads(plan_path.read_bytes())
            self.assertEqual(plan["schema"], STANDARD_FIRST_BIND_PLAN_SCHEMA)
            self.assertIsNone(plan["configured_binding"])
            self.assertEqual(plan["proposed_receipt_policy"], receipt.to_dict())
            self.assertEqual(plan["changed_fields"], ["lifecycle_driver"])
            output, errors = io.StringIO(), io.StringIO()
            status = main(
                [*arguments, str(plan_path), "--apply", "--authorize-rebind"],
                stdout=output,
                stderr=errors,
            )
            self.assertEqual(status, 0, errors.getvalue())
            result = json.loads(output.getvalue())["result"]
            self.assertEqual(result["receipt_policy"], receipt.to_dict())
            self.assertEqual(result["evidence"]["test_receipt"], "stale-until-rebuild")
        current = ProjectConfigurationStore.discover(self.root).definition
        self.assertEqual(current.test_receipt_policy, receipt)
        self.assertIsInstance(current.lifecycle_driver, StandardProjectLifecycleDriver)
        self.assertEqual(historical.read_bytes(), history)

    def test_first_binding_preserves_existing_standard_named_policy(self) -> None:
        receipt = self.receipt_only(standard_suite=True)
        self.assertEqual(self.plan()["proposed_receipt_policy"], receipt.to_dict())
        self.apply(self.plan())
        self.assertEqual(
            ProjectConfigurationStore.discover(
                self.root
            ).definition.test_receipt_policy,
            receipt,
        )

    def test_first_binding_apply_requires_authorization(self) -> None:
        self.receipt_only()
        plan = self.plan()
        before = (self.root / "literate.project.json").read_bytes()
        with self.assertRaises(StandardLifecycleRebindError) as denied:
            self.apply(plan, authorize=False)
        self.assertEqual(
            denied.exception.code, "standard_rebind.authorization_required"
        )
        self.assertEqual((self.root / "literate.project.json").read_bytes(), before)

    def test_first_binding_rejects_rehashed_policy_weakening_and_false_acceptance(
        self,
    ) -> None:
        self.receipt_only()
        plan = self.plan()
        before = (self.root / "literate.project.json").read_bytes()
        for mutation in (
            "suite_id",
            "suite_version",
            "runner_identity",
            "minimum_test_count",
            "required_evidence_kinds",
            "evidence",
            "changed_fields",
        ):
            with self.subTest(mutation=mutation):
                changed = json.loads(json.dumps(plan))
                if mutation == "evidence":
                    changed["evidence_after_apply"]["test_receipt"] = "current"
                elif mutation == "changed_fields":
                    changed["changed_fields"].append("test_receipt_policy")
                else:
                    policy = changed["proposed_receipt_policy"]
                    policy[mutation] = {
                        "suite_id": "foreign-suite",
                        "suite_version": "0.0.1",
                        "runner_identity": canonical_identity(
                            "unauthorized-runner"
                        ).to_dict(),
                        "minimum_test_count": 1,
                        "required_evidence_kinds": [],
                    }[mutation]
                with self.assertRaises(StandardLifecycleRebindError) as denied:
                    self.apply(self.rehash(changed))
                self.assertEqual(denied.exception.code, "standard_rebind.plan_invalid")
                self.assertEqual(
                    (self.root / "literate.project.json").read_bytes(), before
                )

    def test_first_binding_rejects_project_and_installed_authority_drift(self) -> None:
        self.receipt_only()
        plan = self.plan()
        before = (self.root / "literate.project.json").read_bytes()
        with self.assertRaises(StandardLifecycleRebindError) as denied:
            self.apply(plan, observer=lambda: distribution("different"))
        self.assertEqual(
            denied.exception.code, "standard_rebind.installed_authority_changed"
        )
        self.assertEqual((self.root / "literate.project.json").read_bytes(), before)
        store = ProjectConfigurationStore(self.root)
        snapshot = store.discover(self.root)
        store.update(snapshot, replace(snapshot.definition, version="9.0.0"))
        changed = (self.root / "literate.project.json").read_bytes()
        with self.assertRaises(StandardLifecycleRebindError) as denied:
            self.apply(plan)
        self.assertEqual(denied.exception.code, "standard_rebind.project_changed")
        self.assertEqual((self.root / "literate.project.json").read_bytes(), changed)

    def test_first_binding_rolls_back_when_post_write_observation_fails(self) -> None:
        self.receipt_only()
        plan = self.plan()
        before = (self.root / "literate.project.json").read_bytes()
        calls = []

        def observe():
            calls.append(True)
            if len(calls) > 1:
                raise ValueError("installed distribution unavailable")
            return self.new_distribution

        with self.assertRaises(StandardLifecycleRebindError) as denied:
            self.apply(plan, observer=observe)
        self.assertEqual(
            denied.exception.code, "standard_rebind.installed_authority_invalid"
        )
        self.assertEqual((self.root / "literate.project.json").read_bytes(), before)
        self.assertIsNone(
            ProjectConfigurationStore.discover(self.root).definition.lifecycle_driver
        )

    def test_first_binding_cannot_replace_custom_executable_driver(self) -> None:
        custom = ProjectDefinition.from_dict(
            json.loads((ROOT / "literate.project.json").read_bytes())
        )
        store = ProjectConfigurationStore(self.root)
        snapshot = store.discover(self.root)
        store.update(
            snapshot,
            replace(snapshot.definition, lifecycle_driver=custom.lifecycle_driver),
        )
        with self.assertRaises(StandardLifecycleRebindError) as denied:
            self.plan()
        self.assertEqual(denied.exception.code, "standard_rebind.binding_required")

    def test_first_binding_schema_cannot_be_used_for_bound_project(self) -> None:
        plan = self.plan()
        self.assertEqual(plan["schema"], STANDARD_REBIND_PLAN_SCHEMA)
        plan["schema"] = STANDARD_FIRST_BIND_PLAN_SCHEMA
        with self.assertRaises(StandardLifecycleRebindError) as denied:
            self.apply(self.rehash(plan))
        self.assertEqual(denied.exception.code, "standard_rebind.plan_invalid")
        self.receipt_only()
        plan = self.plan()
        plan["schema"] = STANDARD_REBIND_PLAN_SCHEMA
        with self.assertRaises(StandardLifecycleRebindError) as denied:
            self.apply(self.rehash(plan))
        self.assertEqual(denied.exception.code, "standard_rebind.plan_invalid")

    def test_first_binding_does_not_initialize_receiptless_project(self) -> None:
        self.receipt_only()
        store = ProjectConfigurationStore(self.root)
        snapshot = store.discover(self.root)
        store.update(snapshot, replace(snapshot.definition, test_receipt_policy=None))
        with self.assertRaises(StandardLifecycleRebindError) as denied:
            self.plan()
        self.assertEqual(
            denied.exception.code, "standard_rebind.receipt_policy_required"
        )

    def test_first_binding_refuses_changed_installed_policy(self) -> None:
        self.receipt_only()
        plan = self.plan()
        before = (self.root / "literate.project.json").read_bytes()
        changed_policy = replace(
            self.policy, minimum_test_count=self.policy.minimum_test_count + 1
        )
        with self.assertRaises(StandardLifecycleRebindError) as denied:
            self.apply(plan, selected_policy=changed_policy)
        self.assertEqual(
            denied.exception.code, "standard_rebind.installed_authority_changed"
        )
        self.assertEqual((self.root / "literate.project.json").read_bytes(), before)

    def test_bound_rebind_keeps_v1_upgrade_and_byte_preserving_noop(self) -> None:
        plan = self.plan()
        self.assertEqual(plan["schema"], STANDARD_REBIND_PLAN_SCHEMA)
        self.assertEqual(
            plan["changed_fields"], ["lifecycle_driver", "test_receipt_policy"]
        )
        self.apply(plan)
        plan = self.plan()
        self.assertEqual(plan["schema"], STANDARD_REBIND_PLAN_SCHEMA)
        self.assertFalse(plan["change_required"])
        self.assertEqual(plan["changed_fields"], [])
        before = (self.root / "literate.project.json").read_bytes()
        self.assertEqual(self.apply(plan)["action"], "no-op")
        self.assertEqual((self.root / "literate.project.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
