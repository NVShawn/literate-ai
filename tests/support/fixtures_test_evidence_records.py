from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_evidence_records``."""


import hashlib







from literate_ai.contracts.blobs import BlobRef

from literate_ai.contracts.identity import canonical_identity


from literate_ai.security.evidence import (
    DSSE_MEDIA_TYPE,
    DerivationRun,
    EvidenceArtifact,
    EvidenceLocator,
    EvidenceMatrix,
    EvidenceRunContext,
    PlatformRun,
)

def _blob(label: str, media_type: str = "application/json") -> BlobRef:
    content = label.encode()
    return BlobRef(
        hashlib.sha256(content).hexdigest(), len(content), media_type=media_type
    )

def _records():
    context = EvidenceRunContext(
        "run-123",
        "https://example.test/team/project",
        "a" * 40,
        canonical_identity("workflow"),
        canonical_identity("linux-arm64"),
        100,
        200,
    )
    derivation = DerivationRun(
        context,
        _blob("sources"),
        (EvidenceArtifact("specification", _blob("spec")),),
        _blob("journal"),
        "passed",
    )
    platform = PlatformRun(
        context,
        _blob("receipt"),
        _blob("derivation", DSSE_MEDIA_TYPE),
        _blob("environment"),
        (EvidenceArtifact("acceptance", _blob("result")),),
        "passed",
    )
    matrix = EvidenceMatrix(
        context,
        _blob("matrix-plan"),
        ("linux", "windows"),
        (
            EvidenceArtifact("linux", _blob("linux", DSSE_MEDIA_TYPE)),
            EvidenceArtifact("windows", _blob("windows", DSSE_MEDIA_TYPE)),
        ),
    )
    locator = EvidenceLocator("retained-ci", _blob("receipt"), 300)
    return derivation, platform, matrix, locator

