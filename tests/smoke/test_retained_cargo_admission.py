"""Durable retained-Cargo admission requires review, tests and absent source."""

import hashlib
import unittest

from literate_ai.adapters.retained_cargo_admission import (
    read_retained_cargo_retirement_authority,
)
from literate_ai.adapters.retained_cargo_test_authority import (
    read_retained_cargo_test_authority,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.retained_cargo_admission import (
    RetainedCargoSourceRetirement,
)
from literate_ai.contracts.retained_cargo_tests import (
    RetainedCargoTestInventory,
    RetainedCargoTestTarget,
    retained_cargo_test_targets,
)
from tests.support import fixtures_test_retained_cargo_execution as fixtures


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

    def test_retired_source_must_be_absent_and_is_never_deleted(self):
        source = self.root / "retired/provider"
        source.mkdir(parents=True)
        (source / "lib.rs").write_text("preserve")
        with self.assertRaisesRegex(ValueError, "expected-absence-changed"):
            self.authorities()
        self.assertEqual((source / "lib.rs").read_text(), "preserve")
        self.fixture.process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
