"""Transport retained evidence in the canonical directory archive, without admission."""

from literate_ai.adapters.directory_artifacts import (
    DirectoryExportFile,
    encode_directory_export,
    read_directory_export,
)
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity


def encode_qualification_archive(
    entries: tuple[tuple[ContentIdentity, bytes], ...],
    *,
    max_bytes: int,
    max_records: int,
) -> bytes:
    """Carry digest-named record bytes with archive overhead included in bounds."""

    QualificationEvidenceReader(entries, max_bytes=max_bytes, max_records=max_records)
    return encode_directory_export(
        tuple(
            DirectoryExportFile(f"records/{identity.digest}", payload, 0o444)
            for identity, payload in entries
        ),
        max_bytes=max_bytes,
        max_entries=max_records,
    )


def reopen_qualification_archive(
    content: bytes,
    expected: BlobRef,
    *,
    max_bytes: int,
    max_records: int,
) -> QualificationEvidenceReader:
    """Check exact transport bytes and records; caller still supplies current authority.

    Transport must bound input allocation. No extraction, execution, artifact
    selection, or importer trust follows from a valid archive.
    """

    files = read_directory_export(
        content, expected, max_bytes=max_bytes, max_entries=max_records
    )
    entries = []
    for item in files:
        if item.mode != 0o444 or not item.path.startswith("records/"):
            raise QualificationCaptureError("qualification.archive.member-invalid")
        digest = item.path.removeprefix("records/")
        try:
            identity = ContentIdentity.parse_uri(f"sha256:{digest}")
        except ValueError as exc:
            raise QualificationCaptureError(
                "qualification.archive.member-invalid"
            ) from exc
        entries.append((identity, item.content))
    return QualificationEvidenceReader(
        tuple(entries), max_bytes=max_bytes, max_records=max_records
    )
