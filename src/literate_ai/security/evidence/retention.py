"""Detached retention roots and checked graph custody, without receipt promotion."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity, canonical_identity

from .checks import CheckedEvidenceAssertion, EvidenceTrustError
from .dsse import MAX_ENVELOPE_BYTES
from .graph import CheckedRunEvidenceGraph
from .records import DSSE_MEDIA_TYPE, EvidenceLocator


@dataclass(frozen=True, slots=True)
class SignedEvidenceRetention:
    """Exact detached proof bytes and an untrusted routing hint to authenticate.

    These supplied verification roots are retained in the checked result. They are
    not fetched through their own claims or recursively required to sign themselves.
    """

    locator: EvidenceLocator
    envelope: bytes

    def __post_init__(self):
        if (
            not isinstance(self.locator, EvidenceLocator)
            or not isinstance(self.envelope, bytes)
            or not 1 <= len(self.envelope) <= MAX_ENVELOPE_BYTES
        ):
            raise EvidenceTrustError("evidence.retention.claim-invalid")

    @property
    def reference(self) -> BlobRef:
        return BlobRef(
            hashlib.sha256(self.envelope).hexdigest(),
            len(self.envelope),
            media_type=DSSE_MEDIA_TYPE,
        )


@dataclass(frozen=True, slots=True)
class CheckedEvidenceRetention:
    claim: SignedEvidenceRetention
    # One policy check for each run that references this object, including its own
    # envelope and direct child links. A shared object cannot inherit weaker scope.
    runs: tuple[tuple[BlobRef, CheckedEvidenceAssertion], ...]


@dataclass(frozen=True, slots=True)
class CheckedRetainedEvidenceGraph:
    """Authenticated claims and observed retrieval, not future-availability proof.

    This in-memory result does not authorize execution or promote a receipt. The
    caller must preserve detached proof bytes when persisting its evidence roots.
    """

    graph: CheckedRunEvidenceGraph
    retention: tuple[CheckedEvidenceRetention, ...]

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "graph": self.graph.identity.to_dict(),
                "retention": [
                    {
                        "envelope": item.claim.reference.to_dict(),
                        "locator": item.claim.locator.to_dict(),
                        "runs": [
                            [
                                ref.to_dict(),
                                checked.expectation_identity.to_dict(),
                                list(checked.qualified_signer_key_identities),
                            ]
                            for ref, checked in item.runs
                        ],
                    }
                    for item in self.retention
                ],
            }
        )
