"""Durable retained-Cargo admission requires review, tests and absent source."""

import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.retained_cargo_admission import (
    admit_retained_cargo_consumer,
    read_retained_cargo_retirement_authority,
)
from literate_ai.adapters.retained_cargo_execution_inputs import (
    read_retained_cargo_execution_inputs,
)
from literate_ai.adapters.retained_cargo_test_authority import (
    read_retained_cargo_test_authority,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.repositories import RepositoryBuildCommand
from literate_ai.contracts.retained_cargo_admission import (
    RetainedCargoAdmissionReceipt,
    RetainedCargoSourceRetirement,
)
from literate_ai.contracts.retained_cargo_tests import (
    RetainedCargoTestInventory,
    RetainedCargoTestTarget,
    retained_cargo_test_targets,
)
from tests.unit import test_retained_cargo_execution as fixtures


def blob(content):
    return BlobRef(
        hashlib.sha256(content).hexdigest(), len(content), media_type="application/json"
    )


class RetainedCargoAdmissionTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.RetainedCargoExecutionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root = fixture.root
        retirement = RetainedCargoSourceRetirement(
            fixture.importer.project.definition.project_id,
            fixture.materialized._files.binding.identity,
            ("retired/provider",),
        )
        self.retirement_content = canonical_json_bytes(retirement.to_dict())
        (self.root / "retirement.json").write_bytes(self.retirement_content)
        targets = tuple(
            sorted(
                (
                    RetainedCargoTestTarget(
                        package.root,
                        target.name,
                        tuple(sorted(target.kinds)),
                        ("reviewed_case",),
                    )
                    for package, target in retained_cargo_test_targets(
                        fixture.materialized.plan.graph
                    )
                ),
                key=lambda item: item.key,
            )
        )
        inventory = RetainedCargoTestInventory(
            fixture.importer.project.definition.project_id,
            fixture.materialized.plan.identity,
            fixture.importer.gates.identity,
            targets,
        )
        self.inventory_content = canonical_json_bytes(inventory.to_dict())
        (self.root / "tests.json").write_bytes(self.inventory_content)

    def authorities(self):
        retirement = read_retained_cargo_retirement_authority(
            self.fixture.materialized,
            retirement_path="retirement.json",
            reviewed_retirement=blob(self.retirement_content),
        )
        tests = read_retained_cargo_test_authority(
            self.fixture.materialized,
            self.fixture.importer,
            inventory_path="tests.json",
            reviewed_inventory=blob(self.inventory_content),
        )
        return retirement, tests

    def admit(self, **changes):
        retirement, tests = self.authorities()
        inputs = read_retained_cargo_execution_inputs(
            self.fixture.materialized, environment=self.fixture.environment
        )

        def positive_tests(
            authority, metadata, cargo, *, rustc, environment, offline, run
        ):
            authority.require_unchanged()
            run(
                RepositoryBuildCommand(
                    "retained-test-runtime", ("rustc", "--version"), ".", (), False
                ),
                rustc,
            )
            run(
                RepositoryBuildCommand(
                    "retained-test-compile", ("cargo", "test"), ".", (), False
                ),
                cargo,
            )
            for index, _target in enumerate(authority.inventory.targets):
                for phase in ("list", "run"):
                    run(
                        RepositoryBuildCommand(
                            f"retained-test-{index}-{phase}",
                            ("cargo", "test"),
                            ".",
                            (),
                            False,
                        ),
                        cargo,
                    )

        with patch.object(
            fixtures.execution, "observe_retained_cargo_tests", positive_tests
        ):
            return admit_retained_cargo_consumer(
                self.fixture.materialized,
                self.fixture.importer,
                retirement_authority=retirement,
                test_authority=tests,
                cargo=self.fixture.cargo,
                rustc=self.fixture.rustc,
                gate_tools={"make": self.fixture.make},
                environment=self.fixture.environment,
                consumer_inputs=inputs,
                **{
                    "allow_host_execution": True,
                    "acknowledge_source_retirement": True,
                    **changes,
                },
            )

    def test_passing_reviewed_execution_issues_roundtrippable_receipt(self):
        receipt = self.admit()
        self.assertEqual(
            RetainedCargoAdmissionReceipt.from_dict(receipt.to_dict()), receipt
        )
        self.assertTrue(receipt.command_observation_identities)

    def test_acknowledgement_is_required_before_any_process(self):
        with self.assertRaisesRegex(ValueError, "acknowledgement-required"):
            self.admit(acknowledge_source_retirement=False)
        self.fixture.process.assert_not_called()

    def test_retired_source_must_be_absent_and_is_never_deleted(self):
        source = self.root / "retired/provider"
        source.mkdir(parents=True)
        (source / "lib.rs").write_text("preserve")
        with self.assertRaisesRegex(ValueError, "expected-absence-changed"):
            self.authorities()
        self.assertEqual((source / "lib.rs").read_text(), "preserve")
        self.fixture.process.assert_not_called()

    def test_retirement_cannot_overlap_materialized_or_authored_inputs(self):
        for root in (
            self.fixture.materialized._files.binding.destinations[0][1],
            str(
                Path(self.fixture.materialized.plan.workspace_root)
                / self.fixture.materialized.plan.manifests[0].path
            ),
        ):
            retirement = RetainedCargoSourceRetirement(
                self.fixture.importer.project.definition.project_id,
                self.fixture.materialized._files.binding.identity,
                (root,),
            )
            content = canonical_json_bytes(retirement.to_dict())
            (self.root / "retirement.json").write_bytes(content)
            with (
                self.subTest(root=root),
                self.assertRaisesRegex(ValueError, "retirement-overlap"),
            ):
                read_retained_cargo_retirement_authority(
                    self.fixture.materialized,
                    retirement_path="retirement.json",
                    reviewed_retirement=blob(content),
                )

    def test_changed_retirement_review_refuses(self):
        authority, _ = self.authorities()
        (self.root / "retirement.json").write_bytes(self.retirement_content + b"\n")
        with self.assertRaises(ValueError):
            authority.require_unchanged()
        self.fixture.process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
