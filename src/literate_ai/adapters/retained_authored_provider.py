"""Hold actual authored publication, projection and verifier custody for receiving.

This scope grants no importer trust, publication, source retirement or execution.
Authored records remain authored; no source-promotion placeholder is constructed.
"""

import hashlib
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field

from literate_ai.adapters.authored_publication_files import AuthoredPublicationFiles
from literate_ai.adapters.authored_qualification_planning import (
    CurrentAuthoredQualificationPlan,
)
from literate_ai.adapters.authored_qualification_projection import (
    CurrentAuthoredQualificationProjection,
    open_current_authored_qualification_projection,
    reopen_projected_authored_qualification,
    reopen_projected_authored_run,
)
from literate_ai.adapters.authored_qualification_publication import (
    DEFAULT_BYTES,
    MAX_RECORDS,
    PublishedAuthoredQualification,
    _base,
    _blob_path,
    _json,
    _key,
    _retired,
    _stage,
    read_published_authored_qualification,
)
from literate_ai.adapters.qualification_archive import reopen_qualification_archive
from literate_ai.adapters.qualification_capture import QualificationEvidenceReader
from literate_ai.contracts.authored_qualification_reference import (
    AuthoredQualificationReference,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity


@dataclass(frozen=True, slots=True, eq=False)
class RetainedAuthoredProvider:
    """Original operation-local owner, with no decoder or saved grant."""

    current: CurrentAuthoredQualificationPlan
    publication: PublishedAuthoredQualification
    projection: CurrentAuthoredQualificationProjection = field(repr=False)
    _reader: QualificationEvidenceReader = field(repr=False)
    _check: Callable[[object], None] = field(repr=False)

    def require_unchanged(self):
        self._check(self)

    def capture_run(self, *, run_identity, exports_identity, max_package_bytes):
        self.require_unchanged()
        result = reopen_projected_authored_run(
            self.projection,
            self._reader,
            self.publication.reference.products_identity,
            run_identity=run_identity,
            exports_identity=exports_identity,
            max_package_bytes=max_package_bytes,
        )
        self.require_unchanged()
        return result


@contextmanager
def open_retained_authored_provider(
    current,
    attempt,
    *,
    expected_publication_identity,
    native_sdk_inputs=None,
    max_archive_bytes=DEFAULT_BYTES,
):
    """Reopen a committed authored provider under fresh original owners.

    The independent caller selects the exact publication identity. Every guard
    reopens its reference, recovery/retirement chain, stage and complete archive
    bytes, bracketed by current canonical recipe/tool/private-verifier custody.
    Immutable package transport alone cannot recreate this scope after it closes.
    """
    if (
        type(current) is not CurrentAuthoredQualificationPlan
        or not isinstance(expected_publication_identity, ContentIdentity)
    ):
        raise TypeError("original authored plan and independent publication required")
    if type(max_archive_bytes) is not int or max_archive_bytes <= 0:
        raise ValueError("qualification.authored.publication_byte_limit")
    attempt = _key(attempt)
    published = read_published_authored_qualification(
        current,
        attempt,
        expected_identity=expected_publication_identity,
        native_sdk_inputs=native_sdk_inputs,
        max_bytes=max_archive_bytes,
    )
    active = True
    with (
        AuthoredPublicationFiles(current, writable=False) as files,
        open_current_authored_qualification_projection(
            current, native_sdk_inputs=native_sdk_inputs
        ) as projection,
    ):
        def read_current_archive():
            projection.require_unchanged()
            reference = AuthoredQualificationReference.from_dict(
                _json(files, published.reference_path)
            )
            intent, stage, archive = _stage(files, current, attempt, max_archive_bytes)
            if (
                reference != published.reference
                or reference.identity != expected_publication_identity
                or reference.origin != current.plan.origin
                or reference.plan_identity != current.plan.identity
                or reference.intent_identity != canonical_identity(intent)
                or reference.stage_identity != canonical_identity(stage)
                or reference.archive != archive
                or reference.qualification_identity
                != ContentIdentity.from_dict(stage["qualification_identity"])
                or reference.products_identity
                != ContentIdentity.from_dict(stage["products_identity"])
            ):
                raise ValueError("qualification.authored.receiving_publication_changed")
            _retired(files, current, attempt)
            payload = files.read(
                _blob_path(_base(current, attempt), archive),
                max_bytes=max_archive_bytes,
            )
            if (
                len(payload) != archive.size
                or hashlib.sha256(payload).hexdigest() != archive.digest
            ):
                raise ValueError("qualification.authored.receiving_archive_changed")
            files.require_current()
            projection.require_unchanged()
            return payload

        reader = reopen_qualification_archive(
            read_current_archive(),
            published.reference.archive,
            max_bytes=max_archive_bytes,
            max_records=MAX_RECORDS,
        )
        reopened = reopen_projected_authored_qualification(
            projection,
            reader,
            published.reference.products_identity,
            max_package_bytes=max_archive_bytes,
        )
        if (
            reopened.membership.result.identity
            != published.reference.qualification_identity
            or reopened.membership.result.plan != current.plan
        ):
            raise ValueError("qualification.authored.receiving_result_mismatch")

        def check(candidate):
            if not active:
                raise ValueError("qualification.authored.receiving_owner_expired")
            if (
                candidate is not owner
                or candidate.current is not current
                or candidate.publication is not published
                or candidate.projection is not projection
                or candidate._reader is not reader
            ):
                raise ValueError(
                    "qualification.authored.original_receiving_owner_required"
                )
            read_current_archive()

        owner = RetainedAuthoredProvider(current, published, projection, reader, check)
        try:
            owner.require_unchanged()
            yield owner
            owner.require_unchanged()
        finally:
            active = False
