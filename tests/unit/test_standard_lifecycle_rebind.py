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


if __name__ == "__main__":
    unittest.main()
