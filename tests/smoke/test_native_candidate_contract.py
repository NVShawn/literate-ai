"""The existing repair contract admits one exact native ACCEPT failure only."""

import unittest

from literate_ai.contracts.executable_components import (
    CandidateFailureClassification,
    CandidateRepairDiagnostic,
)
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.standard_lifecycle_membership import StandardNodeFailurePhase


class NativeRepairContractTests(unittest.TestCase):
    def diagnostic(self, phase, code, classification):
        return CandidateRepairDiagnostic(
            ContentIdentity.parse_uri("sha256:" + "a" * 64),
            phase,
            code,
            "native public results differ",
            classification,
            ContentIdentity.parse_uri("sha256:" + "b" * 64),
        )

    def test_exact_native_accept_rejection_round_trips(self):
        value = self.diagnostic(
            StandardNodeFailurePhase.ACCEPT,
            "component_acceptance.rust_harness_result_mismatch",
            CandidateFailureClassification.RETRYABLE,
        )
        self.assertEqual(CandidateRepairDiagnostic.from_dict(value.to_dict()), value)

    def test_other_accept_or_wrong_phase_rejection_stays_terminal(self):
        for phase, code in (
            (
                StandardNodeFailurePhase.ACCEPT,
                "component_acceptance.rust_harness_tool_mismatch",
            ),
            (
                StandardNodeFailurePhase.ACCEPT,
                "component_acceptance.rust_harness_archive_custody",
            ),
            (
                StandardNodeFailurePhase.ACCEPT,
                "rust.harness.provisioned-inputs-changed",
            ),
            (
                StandardNodeFailurePhase.ACCEPT,
                "component_acceptance.rust_harness_deadline",
            ),
            (
                StandardNodeFailurePhase.EXECUTE,
                "component_acceptance.rust_harness_result_mismatch",
            ),
        ):
            with self.subTest(phase=phase, code=code):
                with self.assertRaises(ValueError):
                    self.diagnostic(
                        phase, code, CandidateFailureClassification.RETRYABLE
                    )
                terminal = self.diagnostic(
                    phase, code, CandidateFailureClassification.TERMINAL
                )
                self.assertEqual(
                    CandidateRepairDiagnostic.from_dict(terminal.to_dict()), terminal
                )
