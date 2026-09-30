"""One closure's budget cannot be reset by splitting reads across graph edges."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from literate_ai.adapters.evidence_storage import FileSystemEvidenceStore
from literate_ai.application.evidence_resolution import (
    ConfiguredEvidenceResolver,
    EvidenceResolutionSession,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.security.evidence.records import EvidenceLocator
from literate_ai.security.evidence.storage import (
    EvidenceNotFoundError,
    EvidenceReadLimits,
    EvidenceStorageError,
    ResolvedEvidence,
)


def _object(content: bytes) -> ResolvedEvidence:
    reference = BlobRef(hashlib.sha256(content).hexdigest(), len(content))
    return ResolvedEvidence(
        reference, content, EvidenceLocator("local", reference, 100)
    )


class EvidenceResolutionSessionTests(unittest.TestCase):
    def setUp(self):
        self.first = _object(b"1234")
        self.second = _object(b"5678")
        self.third = _object(b"90ab")
        self.backend = Mock()
        self.backend.resolve_many.return_value = (self.first,)

    def _read(self, session, *objects):
        return session.resolve_many(
            tuple(item.reference for item in objects),
            locators=tuple(item.locator for item in objects),
        )

    def _refuses(self, code, call):
        with self.assertRaises(EvidenceStorageError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_cumulative_bytes_reject_before_backend_and_failure_is_terminal(self):
        session = EvidenceResolutionSession(
            self.backend, limits=EvidenceReadLimits(4, 7, 3)
        )
        self._read(session, self.first)
        self._refuses(
            "evidence.storage.total-limit",
            lambda session=session: self._read(session, self.second),
        )
        self.backend.resolve_many.assert_called_once()
        self._refuses(
            "evidence.storage.session-failed",
            lambda session=session: self._read(session, self.first),
        )

    def test_cumulative_objects_reject_even_when_bytes_fit(self):
        session = EvidenceResolutionSession(
            self.backend, limits=EvidenceReadLimits(4, 12, 2)
        )
        self._read(session, self.first)
        self.backend.resolve_many.return_value = (self.second,)
        self._read(session, self.second)
        self._refuses(
            "evidence.storage.object-limit",
            lambda session=session: self._read(session, self.third),
        )
        self.assertEqual(self.backend.resolve_many.call_count, 2)

    def test_duplicate_graph_edges_share_exact_bytes_at_inclusive_limits(self):
        session = EvidenceResolutionSession(
            self.backend, limits=EvidenceReadLimits(4, 8, 3)
        )
        first = self._read(session, self.first)[0]
        self.backend.resolve_many.return_value = (self.second,)
        both = self._read(session, self.first, self.second, self.first)
        self.assertEqual(len(both), 2)
        self.assertIn(first, both)
        self.assertIs(self._read(session, self.first)[0], first)
        self.assertIs(first.content, self.first.content)
        self.assertEqual(self.backend.resolve_many.call_count, 2)
        self.assertEqual(
            self.backend.resolve_many.call_args.args[0], (self.second.reference,)
        )

    def test_cached_digest_cannot_change_size_or_media_on_a_later_edge(self):
        for reference in (
            replace(self.first.reference, size=3),
            replace(self.first.reference, media_type="text/plain"),
        ):
            with self.subTest(reference=reference):
                session = EvidenceResolutionSession(self.backend)
                self._read(session, self.first)
                self._refuses(
                    "evidence.storage.reference-conflict",
                    lambda session=session, reference=reference: session.resolve_many(
                        (reference,), locators=(self.first.locator,)
                    ),
                )

    def test_cached_object_cannot_acquire_new_retention_or_store_claim(self):
        for locator in (
            replace(self.first.locator, retained_until=200),
            replace(self.first.locator, store_id="other"),
        ):
            with self.subTest(locator=locator):
                session = EvidenceResolutionSession(self.backend)
                self._read(session, self.first)
                self._refuses(
                    "evidence.storage.locator-conflict",
                    lambda session=session, locator=locator: session.resolve_many(
                        (self.first.reference,), locators=(locator,)
                    ),
                )

    def test_partial_duplicate_extra_or_wrong_backend_objects_fail_closed(self):
        for returned in (
            (),
            (self.first, self.first),
            (self.first, self.second, self.third),
            (self.first, self.third),
            [self.first, self.second],
            (self.first, object()),
            (
                self.first,
                replace(
                    self.second, locator=replace(self.second.locator, store_id="other")
                ),
            ),
        ):
            with self.subTest(returned=returned):
                backend = Mock()
                backend.resolve_many.return_value = returned
                session = EvidenceResolutionSession(backend)
                self._refuses(
                    "evidence.storage.resolution-mismatch",
                    lambda session=session: self._read(
                        session, self.first, self.second
                    ),
                )
                self._refuses(
                    "evidence.storage.session-failed",
                    lambda session=session: self._read(session, self.first),
                )
                backend.resolve_many.assert_called_once()

    def test_port_bytes_are_reverified_even_for_preconstructed_result(self):
        corrupted = _object(b"1234")
        object.__setattr__(corrupted, "content", b"evil")
        self.backend.resolve_many.return_value = (corrupted,)
        session = EvidenceResolutionSession(self.backend)
        self._refuses(
            "evidence.storage.digest-mismatch",
            lambda session=session: self._read(session, self.first),
        )

    def test_backend_errors_and_interruptions_cannot_leave_reusable_partial_session(
        self,
    ):
        for error, code in (
            (EvidenceNotFoundError(), "evidence.storage.not-found"),
            (
                RuntimeError("private backend details"),
                "evidence.storage.backend-failed",
            ),
            (KeyboardInterrupt(), None),
        ):
            with self.subTest(error=type(error)):
                backend = Mock()
                backend.resolve_many.side_effect = error
                session = EvidenceResolutionSession(backend)
                if code is None:
                    with self.assertRaises(KeyboardInterrupt):
                        self._read(session, self.first)
                else:
                    self._refuses(
                        code, lambda session=session: self._read(session, self.first)
                    )
                self._refuses(
                    "evidence.storage.session-failed",
                    lambda session=session: self._read(session, self.first),
                )

    def test_real_filesystem_resolver_retains_bytes_without_rereading_old_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FileSystemEvidenceStore(Path(directory).resolve(), writable=True)
            for item in (self.first, self.second):
                self.assertEqual(
                    store.put_bytes(item.content, media_type=item.reference.media_type),
                    item.reference,
                )
            backend = ConfiguredEvidenceResolver({"local": store})
            session = EvidenceResolutionSession(
                backend, limits=EvidenceReadLimits(4, 8, 2)
            )
            self._read(session, self.first)
            # Availability must be evaluated separately by admission; retained exact
            # bytes prevent a second read substituting what an earlier edge consumed.
            path = (
                Path(directory)
                / "blobs"
                / "sha256"
                / self.first.reference.digest[:2]
                / self.first.reference.digest
            )
            path.unlink()
            result = self._read(session, self.first, self.second)
            self.assertEqual({item.content for item in result}, {b"1234", b"5678"})
            with self.assertRaises(EvidenceNotFoundError):
                backend.resolve_many(
                    (self.first.reference,), locators=(self.first.locator,)
                )


if __name__ == "__main__":
    unittest.main()
