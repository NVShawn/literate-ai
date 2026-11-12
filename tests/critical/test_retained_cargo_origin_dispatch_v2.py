"""Origin separation under real source-derived archive/publication fixtures.

The malformed authored DTO below is a negative input only. It never supplies
authored authority, executes a workspace or produces an admission receipt.
"""

import unittest

from literate_ai.adapters.authored_qualification_capture import (
    reopen_authored_product_membership,
)
from literate_ai.adapters.qualification_archive import reopen_qualification_archive
from literate_ai.adapters.retained_authored_cargo_import_v2 import (
    RetainedAuthoredCargoCurrentInputsV2,
)
from literate_ai.adapters.retained_cargo_import_v2 import (
    verify_retained_cargo_archive_v2,
)
from tests.critical import test_retained_cargo_receiving_v2 as fixtures


@unittest.skipUnless(
    fixtures.os.name != "nt"
    and fixtures.shutil.which("git")
    and fixtures.shutil.which("make"),
    "requires native POSIX adoption and Git",
)
class RetainedCargoOriginDispatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RetainedCargoReceivingV2Tests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def malformed_authored(self):
        # This incomplete value must be rejected on exact origin before any
        # authored current/provider methods or other missing fields are read.
        value = object.__new__(RetainedAuthoredCargoCurrentInputsV2)
        object.__setattr__(value, "reviewed", self.fixture.reviewed)
        return value

    def test_source_archive_final_read_cannot_switch_to_authored_origin(self):
        f = self.fixture
        calls = 0

        def current():
            nonlocal calls
            calls += 1
            return f.current if calls == 1 else self.malformed_authored()

        with self.assertRaisesRegex(TypeError, "v2-current-inputs-required"):
            verify_retained_cargo_archive_v2(
                **{**f.provider.calls, "read_current": current}
            )
        self.assertEqual(calls, 2)
        self.assertFalse(
            any(
                (f.reviewed.project.root / path).exists()
                for _, path in f.reviewed.binding.common.destinations
            )
        )

    def test_materialized_source_owner_cannot_change_its_origin(self):
        f = self.fixture
        selected = f.current
        materialized = f.receive(read_current=lambda: selected)
        selected = self.malformed_authored()
        with self.assertRaisesRegex(ValueError, "current-inputs-changed"):
            materialized.require_unchanged()
        self.assertTrue(all(tree.root.is_dir() for tree in materialized.packages))

    def test_source_qualification_record_is_not_authored_product_membership(self):
        f = self.fixture
        binding = f.reviewed.binding.common
        payload = f.provider.read_archive(
            binding.source_store_id, binding.qualification_archive, 5_000_000
        )
        reader = reopen_qualification_archive(
            payload,
            binding.qualification_archive,
            max_bytes=5_000_000,
            max_records=1000,
        )
        with self.assertRaises((TypeError, ValueError)):
            reopen_authored_product_membership(reader, binding.qualification_identity)

    def test_passive_inputs_cannot_construct_authored_current_snapshot(self):
        with self.assertRaisesRegex(TypeError, "authored-current-inputs-required"):
            RetainedAuthoredCargoCurrentInputsV2(
                self.fixture.reviewed,
                {},
                self.fixture.reviewed.binding.identity,
                "reviewed-store",
                object(),
                object(),
                self.fixture.reviewed.plan.common.gates,
            )
