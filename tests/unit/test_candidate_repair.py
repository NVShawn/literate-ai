"""Versioned bounded replacement-attempt contracts."""

from __future__ import annotations

import unittest
from dataclasses import replace

from literate_ai.contracts.executable_components import (
    CandidateAttempt,
    CandidateAttemptChain,
    CandidateAttemptChainDisposition,
    CandidateAttemptDisposition,
    CandidateFailureClassification,
    CandidateRepairDiagnostic,
    CandidateRepairRequest,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.standard_lifecycle_membership import (
    StandardNodeFailureEvidence,
    StandardNodeFailurePhase,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


def _identity(label: str):
    return canonical_identity({"candidate-repair-fixture": label})


class CandidateRepairContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.component = _identity("component")
        failure = StandardNodeFailureEvidence(
            self.component,
            StandardNodeFailurePhase.BUILD,
            "builder.generated-source-rejected",
            _identity("failed-build"),
        )
        self.diagnostic = CandidateRepairDiagnostic(
            self.component,
            failure.phase,
            failure.code,
            "compiler rejected generated declaration at relative/file.cc:12",
            CandidateFailureClassification.RETRYABLE,
            failure.identity,
        )
        self.first = CandidateAttempt(
            self.component,
            0,
            _identity("request-0"),
            _identity("response-0"),
            _identity("tree-0"),
            _identity("bundle-0"),
            _identity("workspace-0"),
            _identity("result-0"),
            (),
            CandidateAttemptDisposition.RETRYABLE_REJECTED,
            self.diagnostic,
        )
        repair = CandidateRepairRequest(
            self.component,
            1,
            _identity("recipe"),
            (self.first.identity,),
            self.diagnostic.identity,
            _identity("request-1"),
            _identity("workspace-1"),
        )
        self.second = CandidateAttempt(
            self.component,
            1,
            repair.bounded_generation_request_identity,
            _identity("response-1"),
            _identity("tree-1"),
            _identity("bundle-1"),
            repair.workspace_allocation_identity,
            _identity("result-1"),
            (self.first.identity,),
            CandidateAttemptDisposition.ACCEPTED,
            None,
            repair,
        )
        self.repair = repair
        self.chain = CandidateAttemptChain(
            self.component,
            (self.first, self.second),
            CandidateAttemptChainDisposition.ACCEPTED,
        )
        self.schemas = SchemaCatalog()

    def test_contracts_round_trip_and_validate_against_catalog(self) -> None:
        for value in (
            self.diagnostic,
            self.repair,
            self.first,
            self.second,
            self.chain,
        ):
            with self.subTest(schema=value.SCHEMA):
                self.schemas.validate(value.SCHEMA, value.to_dict())
                decoded = type(value).from_dict(value.to_dict())
                self.assertEqual(decoded, value)
                self.assertEqual(decoded.identity, value.identity)

    def test_predecessor_substitution_and_workspace_reuse_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "exact replacement attempt"):
            CandidateAttemptChain(
                self.component,
                (
                    self.first,
                    replace(
                        self.second,
                        predecessor_attempt_identities=(_identity("other"),),
                    ),
                ),
                CandidateAttemptChainDisposition.ACCEPTED,
            )
        with self.assertRaisesRegex(ValueError, "fresh workspace"):
            CandidateAttemptChain(
                self.component,
                (
                    self.first,
                    replace(
                        self.second,
                        workspace_allocation_identity=self.first.workspace_allocation_identity,
                        repair_request=replace(
                            self.second.repair_request,
                            workspace_allocation_identity=(
                                self.first.workspace_allocation_identity
                            ),
                        ),
                    ),
                ),
                CandidateAttemptChainDisposition.ACCEPTED,
            )

    def test_retryability_is_strict_and_diagnostics_are_sanitized(self) -> None:
        with self.assertRaisesRegex(ValueError, "only compiler/linker"):
            replace(self.diagnostic, phase=StandardNodeFailurePhase.BUILD_AUTHORIZATION)
        for unsafe in (
            "compiler read /private/source.cc",
            "password=hunter2",
            "Bearer abcdef",
            "C:\\Users\\person\\source.cc",
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaisesRegex(ValueError, "exclude absolute paths"):
                    replace(self.diagnostic, sanitized_text=unsafe)

    def test_chain_enforces_two_repair_limit_and_terminal_shape(self) -> None:
        with self.assertRaisesRegex(ValueError, "at most two repairs"):
            CandidateAttemptChain(
                self.component,
                (self.first, self.second, self.second, self.second),
                CandidateAttemptChainDisposition.ACCEPTED,
            )
        terminal = replace(
            self.first,
            disposition=CandidateAttemptDisposition.TERMINAL_REJECTED,
            diagnostic=replace(
                self.diagnostic,
                classification=CandidateFailureClassification.TERMINAL,
            ),
        )
        chain = CandidateAttemptChain(
            self.component,
            (terminal,),
            CandidateAttemptChainDisposition.TERMINAL,
        )
        self.assertEqual(
            chain.attempts[-1].lifecycle_result_identity, _identity("result-0")
        )


if __name__ == "__main__":
    unittest.main()
