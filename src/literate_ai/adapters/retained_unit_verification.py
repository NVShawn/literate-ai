"""Explicit original-result selection; decoding never supplies live admission."""

from dataclasses import dataclass

from literate_ai.adapters.action_filesystem_blob_source import (
    FileSystemActionBlobSource,
)
from literate_ai.adapters.retained_original_unit_evidence import (
    ORIGINAL_VERIFICATION_MODE,
    OriginalRetainedUnitEvidence,
)
from literate_ai.adapters.retained_unit_results import (
    read_retained_unit_result_index,
    read_retained_unit_results,
)
from literate_ai.contracts import ContentIdentity
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.retained_unit_policy import RetainedUnitPackagePolicy
from literate_ai.storage.cas import FileSystemCAS

VERIFICATION_MODE = "fresh-exact-projection"


@dataclass(frozen=True, slots=True)
class RetainedUnitVerificationSelection:
    """Reviewed intent only, including private receiver assertions without a source."""

    result: BlobRef
    custody_identity: ContentIdentity
    source: FileSystemActionBlobSource | None = None
    original_evidence: OriginalRetainedUnitEvidence | None = None

    def __post_init__(self):
        if (
            not isinstance(self.result, BlobRef)
            or self.result.media_type != "application/json"
            or self.result.size <= 0
            or not isinstance(self.custody_identity, ContentIdentity)
            or self.original_evidence is not None
            and (
                not isinstance(self.original_evidence, OriginalRetainedUnitEvidence)
                or self.original_evidence.result_index != self.result
            )
            or self.source is not None
            and (
                not isinstance(self.source, FileSystemActionBlobSource)
                or self.source.identity != self.custody_identity
            )
        ):
            raise TypeError("retained.unit.explicit-verification-selection-required")

    def to_dict(self):
        result = {
            "verification_mode": (
                VERIFICATION_MODE
                if self.original_evidence is None
                else ORIGINAL_VERIFICATION_MODE
            ),
            "result_index": self.result.to_dict(),
            "source_custody_identity": self.custody_identity.uri,
        }
        if self.original_evidence is not None:
            result["original_evidence"] = self.original_evidence.to_dict()
        return result

    @classmethod
    def from_dict(cls, value):
        """Decode only explicit intent; no transport or admission is restored."""
        base = {"verification_mode", "result_index", "source_custody_identity"}
        if type(value) is not dict:
            raise ValueError("retained.unit.closed-verification-selection-required")
        original = value.get("verification_mode") == ORIGINAL_VERIFICATION_MODE
        if set(value) != (base | {"original_evidence"} if original else base) or value[
            "verification_mode"
        ] != (ORIGINAL_VERIFICATION_MODE if original else VERIFICATION_MODE):
            raise ValueError("retained.unit.closed-verification-selection-required")
        return cls(
            BlobRef.from_dict(value["result_index"]),
            ContentIdentity.parse_uri(value["source_custody_identity"]),
            original_evidence=(
                OriginalRetainedUnitEvidence.from_dict(value["original_evidence"])
                if original
                else None
            ),
        )

    def require_current(self, policy):
        if not isinstance(policy, RetainedUnitPackagePolicy):
            raise TypeError("retained.unit.explicit-verification-policy-required")
        if self.original_evidence is not None:
            self.original_evidence.require_budget(policy)
        if self.result.size > policy.max_index_bytes:
            raise ValueError("retained.unit.verification-index-budget-exceeded")
        if self.source is not None:
            self.source.require_current()
            index = read_retained_unit_result_index(
                self.result,
                self.source.store,
                max_descriptor_bytes=policy.max_descriptor_bytes,
                max_descriptor_total_bytes=policy.max_descriptor_total_bytes,
                max_index_bytes=policy.max_index_bytes,
                max_metadata_memory_bytes=policy.max_metadata_memory_bytes,
            )
            if (
                index.unit_count > policy.max_units
                or index.archive_bytes > policy.max_total_archive_bytes
            ):
                raise ValueError("retained.unit.verification-set-budget-exceeded")
            self.source.require_current()

    def require_original_source(
        self, projection, policy, *, admission_guard, cancelled=lambda: False
    ):
        """A valid destination cache cannot hide loss in the selected source."""
        self.require_current(policy)
        if self.source is None:
            raise ValueError("retained.unit.original-source-custody-required")
        source = self.source

        def current():
            source.require_current()
            if cancelled():
                raise ValueError("retained.unit.original-source-verification-cancelled")
            admission_guard()
            source.require_current()

        class SelectedMetadataCAS(FileSystemCAS):
            def get_bytes(self, reference):
                current()
                content = super().get_bytes(reference)
                current()
                return content

        class HashSink:
            def write(self, content):
                return len(content)

        _index, results = read_retained_unit_results(
            projection,
            self.result,
            SelectedMetadataCAS(source.store.root, create=False),
            max_descriptor_bytes=policy.max_descriptor_bytes,
            max_descriptor_total_bytes=policy.max_descriptor_total_bytes,
            max_index_bytes=policy.max_index_bytes,
            max_metadata_memory_bytes=policy.max_metadata_memory_bytes,
        )
        for result in results:
            source.copy_to(
                result.archive,
                HashSink(),
                maximum_bytes=policy.max_archive_bytes,
                admission_guard=current,
                cancelled=cancelled,
            )
        current()
