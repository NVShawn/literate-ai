"""Closed provider artifact transfer descriptors and action-wide bounds."""

from dataclasses import dataclass

from literate_ai.adapters.action_build_limits import (
    MAX_BUILD_ARCHIVE_BYTES,
    MAX_BUILD_EVIDENCE_BYTES,
    MAX_BUILD_EVIDENCE_RECORDS,
)
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
)
from literate_ai.adapters.source_evidence_validation import (
    SourceEvidenceValidationInputs,
)
from literate_ai.contracts import ContentIdentity, StandardComponentAcceptanceEvidence
from literate_ai.contracts.blobs import BlobRef


def _invalid():
    raise ActionWireError(
        "action_build.provider_invalid", "provider build transfer differs"
    )


@dataclass(frozen=True, slots=True)
class ProviderBuildTransfer:
    receipt_identity: ContentIdentity
    artifact_archive: BlobRef
    evidence_records: tuple[BlobRef, ...]
    source_validation: SourceEvidenceValidationInputs

    def require_receipt(self, receipt):
        if not isinstance(receipt, StandardComponentAcceptanceEvidence):
            _invalid()
        refs = self.evidence_records
        if (
            not isinstance(self.source_validation, SourceEvidenceValidationInputs)
            or self.receipt_identity != receipt.identity
            or not isinstance(self.artifact_archive, BlobRef)
            or self.artifact_archive.size > MAX_BUILD_ARCHIVE_BYTES
            or not isinstance(refs, tuple)
            or not 1 <= len(refs) <= MAX_BUILD_EVIDENCE_RECORDS
            or any(not isinstance(item, BlobRef) for item in refs)
            or any(item.size > MAX_ACTION_RECORD_BYTES for item in refs)
            or sum(item.size for item in refs) > MAX_BUILD_EVIDENCE_BYTES
        ):
            _invalid()
        identities = tuple(item.identity for item in refs)
        if identities != tuple(sorted(set(identities))) or any(
            identity.uri not in identities
            for identity in (
                receipt.identity,
                receipt.build.identity,
                receipt.build.build_plan_identity,
                receipt.generated_tests.identity,
                receipt.execution.identity,
                receipt.acceptance_policy_identity,
                receipt.build.source_custody_identity,
            )
        ):
            _invalid()

    def to_dict(self):
        return {
            "schema": "literate-ai/provider-build-transfer@1",
            "receipt_identity": self.receipt_identity.uri,
            "artifact_archive": self.artifact_archive.to_dict(),
            "source_validation": self.source_validation.to_dict(),
            "evidence_records": [item.to_dict() for item in self.evidence_records],
        }

    @classmethod
    def from_dict(cls, value):
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema",
                "receipt_identity",
                "artifact_archive",
                "evidence_records",
                "source_validation",
            }
            or value["schema"] != "literate-ai/provider-build-transfer@1"
            or not isinstance(value["evidence_records"], list)
            or not 1 <= len(value["evidence_records"]) <= MAX_BUILD_EVIDENCE_RECORDS
        ):
            _invalid()
        return cls(
            ContentIdentity.parse_uri(value["receipt_identity"]),
            BlobRef.from_dict(value["artifact_archive"]),
            tuple(BlobRef.from_dict(item) for item in value["evidence_records"]),
            SourceEvidenceValidationInputs.from_dict(value["source_validation"]),
        )


def validate_provider_transfers(receipts, transfers):
    if not isinstance(transfers, tuple) or len(transfers) != len(receipts):
        _invalid()
    archives, records, all_refs = {}, {}, {}
    for receipt, transfer in zip(receipts, transfers, strict=True):
        if not isinstance(transfer, ProviderBuildTransfer):
            _invalid()
        transfer.require_receipt(receipt)
        for inventory, references in (
            (archives, (transfer.artifact_archive,)),
            (records, transfer.evidence_records),
        ):
            for reference in references:
                previous = all_refs.setdefault(reference.identity, reference)
                inventory.setdefault(reference.identity, reference)
                if previous != reference:
                    _invalid()
    if (
        sum(item.size for item in archives.values()) > MAX_BUILD_ARCHIVE_BYTES
        or len(records) > MAX_BUILD_EVIDENCE_RECORDS
        or sum(item.size for item in records.values()) > MAX_BUILD_EVIDENCE_BYTES
    ):
        _invalid()
