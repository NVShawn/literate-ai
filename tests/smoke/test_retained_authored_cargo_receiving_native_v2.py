"""Actual authored-origin consumer admission; producer qualification is injected.

No authored model, generation, publication or original native provider is qualified
by this fixture. Complete archive checks and retained source/scratch/tool custody,
Cargo metadata/build/test processes and the resulting consumer receipt are real.
"""

import os
import shutil
import unittest
from unittest.mock import patch

from literate_ai.adapters import retained_cargo_execution_v2 as executor
from literate_ai.adapters.retained_cargo_admission_v2 import (
    admit_retained_cargo_consumer_v2,
)
from literate_ai.adapters.retained_cargo_execution import RetainedCargoExecutionError
from tests.smoke import test_retained_cargo_receiving_native_v2 as source_receiving
from tests.support import fixtures_retained_authored_cargo as authored_fixtures


@unittest.skipUnless(
    os.name != "nt" and all(shutil.which(x) for x in ("cargo", "rustc", "make", "git")),
    "requires native POSIX Cargo/Rust and retained Make/Git adoption",
)
class RetainedAuthoredCargoNativeReceivingV2Tests(unittest.TestCase):
    fixture_type = authored_fixtures.AuthoredNativeReceivingFixture
    # Reuse the ordinary receiver setup, without inheriting or replaying its tests.
    open_case = source_receiving.RetainedCargoNativeReceivingV2Tests.open_case
    retirement = source_receiving.RetainedCargoNativeReceivingV2Tests.retirement
    execution = source_receiving.RetainedCargoNativeReceivingV2Tests.execution

    def current_inputs(self, fixture):
        return fixture.authored.current(self.reviewed)

    def test_real_authored_archive_materialization_gates_and_retired_admission(self):
        self.open_case()
        receipt = admit_retained_cargo_consumer_v2(
            self.materialized,
            retirement_authority=self.retirement(),
            acknowledge_source_retirement=True,
            **self.execution(timeout_seconds=2400),
        )
        self.assertEqual(
            receipt.common.qualification_identity,
            self.fixture.authored.reference.qualification_identity,
        )
        self.assertEqual(
            tuple(outcome.key for outcome in receipt.target_outcomes),
            tuple(target.key for target in self.reviewed.inventory.targets),
        )
        self.assertEqual(
            {outcome.kind for outcome in receipt.target_outcomes},
            {"libtest", "custom-harness", "excluded"},
        )
        self.assertFalse(self.original_path.exists())
        self.assertEqual(self.original_bytes, b"print('retained')\n")
        self.assertTrue(self.fixture.authored.state.reads)
        self.materialized.require_unchanged()

    def test_authored_owner_expiry_after_real_gate_cannot_return_a_receipt(self):
        self.open_case()
        real = executor.run_bounded_process
        completed = []

        def expire_after_gate(argv, **arguments):
            result = real(argv, **arguments)
            completed.append((tuple(argv), result.returncode))
            if tuple(argv[1:3]) == ("check", "--workspace"):
                self.assertEqual(result.returncode, 0)
                self.fixture.authored.state.active = False
            return result

        with (
            patch.object(executor, "run_bounded_process", expire_after_gate),
            self.assertRaises(RetainedCargoExecutionError) as caught,
        ):
            admit_retained_cargo_consumer_v2(
                self.materialized,
                retirement_authority=self.retirement(),
                acknowledge_source_retirement=True,
                **self.execution(),
            )
        self.assertTrue(
            any(
                argv[1:3] == ("check", "--workspace") and code == 0
                for argv, code in completed
            )
        )
        self.assertIsInstance(caught.exception.__cause__, ValueError)
        self.assertIn(
            "original authored owner expired", str(caught.exception.__cause__)
        )
        self.assertFalse(any("--list" in argv for argv, _code in completed))
        self.assertFalse(self.fixture.authored.state.active)
        self.assertFalse(self.original_path.exists())
