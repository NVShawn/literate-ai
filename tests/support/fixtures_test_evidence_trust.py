"""Shared test fixtures extracted from test_evidence_trust."""

from __future__ import annotations

from literate_ai.security.evidence import (
    STATEMENT_MEDIA_TYPE,
    Ed25519EvidenceSigner,
    EvidenceMatrix,
    EvidenceStatement,
    RunEvidenceExpectation,
)
from tests.support.fixtures_test_evidence_records import _records


def _signer():
    return Ed25519EvidenceSigner(bytes(range(32)))


def _expectation(record=None):
    # Derive only the fixed test baseline here. Mutation tests retain this baseline
    # while changing and legitimately re-signing the producer's assertion.
    record = record or _records()[0]
    c = record.context
    return RunEvidenceExpectation(
        record.SCHEMA,
        record.subject,
        c.invocation_id,
        c.repository,
        c.revision,
        c.workflow,
        c.target,
        90,
        220,
        record.required_cells if isinstance(record, EvidenceMatrix) else (),
    )


def _envelope(record, signer=None):
    return (signer or _signer()).sign(
        STATEMENT_MEDIA_TYPE, EvidenceStatement(record).to_bytes()
    )
