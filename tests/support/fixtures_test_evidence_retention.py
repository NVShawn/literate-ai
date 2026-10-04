from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_evidence_retention``."""


from literate_ai.security.evidence import (
    STATEMENT_MEDIA_TYPE,
    EvidenceStatement,
)
from literate_ai.security.evidence.retention import SignedEvidenceRetention


def _claims(graph, *, signer=None, locators=None):
    signer = signer or graph.signer
    return tuple(
        SignedEvidenceRetention(
            locator,
            signer.sign(
                STATEMENT_MEDIA_TYPE, EvidenceStatement(locator).to_bytes()
            ).to_bytes(),
        )
        for locator in (graph.locators() if locators is None else locators)
    )
