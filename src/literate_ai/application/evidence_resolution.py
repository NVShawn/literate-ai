"""Resolve required evidence through configured ports, without receipt admission."""

from __future__ import annotations

from collections.abc import Mapping
from threading import Lock
from types import MappingProxyType

from literate_ai.contracts.blobs import BlobRef
from literate_ai.security.evidence.records import (
    DerivationRun,
    EvidenceLocator,
    EvidenceMatrix,
    PlatformRun,
)
from literate_ai.security.evidence.statements import EvidenceStatement
from literate_ai.security.evidence.storage import (
    DEFAULT_EVIDENCE_READ_LIMITS,
    EvidenceNotFoundError,
    EvidenceReadLimits,
    EvidenceResolver,
    EvidenceStorageError,
    EvidenceStore,
    ResolvedEvidence,
)


class ConfiguredEvidenceResolver:
    """Snapshot routing, reject ambiguity before I/O, and verify every returned byte.

    Missing mirrors may be tried in store-ID order. Corruption and other failures
    are not hidden by fallback. Retention deadlines remain assertions, not proof.
    """

    def __init__(
        self,
        stores: Mapping[str, EvidenceStore],
        *,
        limits: EvidenceReadLimits = DEFAULT_EVIDENCE_READ_LIMITS,
    ):
        if (
            not isinstance(limits, EvidenceReadLimits)
            or not stores
            or len(stores) > limits.maximum_objects
        ):
            raise EvidenceStorageError("evidence.storage.configuration-invalid")
        # The locator constructor validates the same portable IDs used on the wire.
        empty = BlobRef(
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", 0
        )
        for store_id in stores:
            EvidenceLocator(store_id, empty, 0)
        self._stores = MappingProxyType(dict(stores))
        self._limits = limits

    def resolve_many(
        self, references: tuple[BlobRef, ...], *, locators: tuple[EvidenceLocator, ...]
    ) -> tuple[ResolvedEvidence, ...]:
        limits = self._limits
        if (
            not isinstance(references, tuple)
            or not 1 <= len(references) <= limits.maximum_objects
            or not isinstance(locators, tuple)
            or not 1 <= len(locators) <= limits.maximum_objects
        ):
            raise EvidenceStorageError("evidence.storage.object-limit")
        expected: dict[str, BlobRef] = {}
        for reference in references:
            limits.require_reference(reference)
            previous = expected.setdefault(reference.identity, reference)
            if previous != reference:
                raise EvidenceStorageError("evidence.storage.reference-conflict")
        if sum(item.size for item in expected.values()) > limits.maximum_total_bytes:
            raise EvidenceStorageError("evidence.storage.total-limit")
        located: dict[tuple[str, str], EvidenceLocator] = {}
        for locator in locators:
            if not isinstance(locator, EvidenceLocator):
                raise EvidenceStorageError("evidence.storage.locator-invalid")
            if locator.store_id not in self._stores:
                raise EvidenceStorageError("evidence.storage.store-unconfigured")
            key = (locator.subject.identity, locator.store_id)
            previous = located.setdefault(key, locator)
            if previous != locator:
                raise EvidenceStorageError("evidence.storage.locator-conflict")
            if (
                locator.subject.identity in expected
                and locator.subject != expected[locator.subject.identity]
            ):
                raise EvidenceStorageError("evidence.storage.locator-mismatch")
        candidates: dict[str, list[EvidenceLocator]] = {
            identity: [] for identity in expected
        }
        for (identity, _store), locator in sorted(located.items()):
            if identity in candidates:
                candidates[identity].append(locator)
        if any(not choices for choices in candidates.values()):
            raise EvidenceNotFoundError()
        result = []
        for identity, reference in sorted(expected.items()):
            for locator in candidates[identity]:
                try:
                    content = self._stores[locator.store_id].get_bytes(reference)
                except EvidenceNotFoundError:
                    continue
                except EvidenceStorageError:
                    raise
                except Exception:
                    raise EvidenceStorageError(
                        "evidence.storage.backend-failed"
                    ) from None
                result.append(ResolvedEvidence(reference, content, locator))
                break
            else:
                raise EvidenceNotFoundError()
        return tuple(result)


class EvidenceResolutionSession:
    """Retain verified objects under one budget across successive graph reads.

    Create one session per closure verification, not per graph node. Calls serialize;
    any failed call permanently closes the session. Cached bytes establish integrity
    only, not current availability, signer trust, or retention authorization.
    """

    def __init__(
        self,
        resolver: EvidenceResolver,
        *,
        limits: EvidenceReadLimits = DEFAULT_EVIDENCE_READ_LIMITS,
    ):
        if not isinstance(limits, EvidenceReadLimits):
            raise EvidenceStorageError("evidence.storage.configuration-invalid")
        self._resolver = resolver
        self._limits = limits
        self._objects: dict[str, ResolvedEvidence] = {}
        self._total_bytes = 0
        self._failed = False
        self._lock = Lock()

    def resolve_many(
        self, references: tuple[BlobRef, ...], *, locators: tuple[EvidenceLocator, ...]
    ) -> tuple[ResolvedEvidence, ...]:
        with self._lock:
            if self._failed:
                raise EvidenceStorageError("evidence.storage.session-failed")
            try:
                return self._resolve(references, locators)
            except BaseException:
                # A caller cannot recover a partial graph by ignoring a failed edge.
                self._failed = True
                raise

    def _resolve(
        self, references: tuple[BlobRef, ...], locators: tuple[EvidenceLocator, ...]
    ) -> tuple[ResolvedEvidence, ...]:
        limits = self._limits
        if (
            not isinstance(references, tuple)
            or not 1 <= len(references) <= limits.maximum_objects
            or not isinstance(locators, tuple)
            or not 1 <= len(locators) <= limits.maximum_objects
        ):
            raise EvidenceStorageError("evidence.storage.object-limit")
        expected: dict[str, BlobRef] = {}
        for reference in references:
            limits.require_reference(reference)
            previous = expected.setdefault(reference.identity, reference)
            cached = self._objects.get(reference.identity)
            if previous != reference or (
                cached is not None and cached.reference != reference
            ):
                raise EvidenceStorageError("evidence.storage.reference-conflict")
        pending = {
            identity: reference
            for identity, reference in expected.items()
            if identity not in self._objects
        }
        if len(self._objects) + len(pending) > limits.maximum_objects:
            raise EvidenceStorageError("evidence.storage.object-limit")
        new_bytes = sum(item.size for item in pending.values())
        if self._total_bytes + new_bytes > limits.maximum_total_bytes:
            raise EvidenceStorageError("evidence.storage.total-limit")
        located: dict[tuple[str, str], EvidenceLocator] = {}
        covered: set[str] = set()
        for locator in locators:
            if not isinstance(locator, EvidenceLocator):
                raise EvidenceStorageError("evidence.storage.locator-invalid")
            identity = locator.subject.identity
            key = (identity, locator.store_id)
            if located.setdefault(key, locator) != locator:
                raise EvidenceStorageError("evidence.storage.locator-conflict")
            known = expected.get(identity)
            cached = self._objects.get(identity)
            if known is None and cached is not None:
                known = cached.reference
            if known is not None and known != locator.subject:
                raise EvidenceStorageError("evidence.storage.locator-mismatch")
            if identity in expected:
                covered.add(identity)
        if covered != set(expected):
            raise EvidenceNotFoundError()
        for identity in expected.keys() - pending.keys():
            retained = self._objects[identity].locator
            if located.get((identity, retained.store_id)) != retained:
                raise EvidenceStorageError("evidence.storage.locator-conflict")
        if pending:
            try:
                resolved = self._resolver.resolve_many(
                    tuple(pending[key] for key in sorted(pending)),
                    locators=locators,
                )
            except EvidenceStorageError:
                raise
            except Exception:
                raise EvidenceStorageError("evidence.storage.backend-failed") from None
            if not isinstance(resolved, tuple) or len(resolved) != len(pending):
                raise EvidenceStorageError("evidence.storage.resolution-mismatch")
            received: dict[str, ResolvedEvidence] = {}
            for item in resolved:
                if not isinstance(item, ResolvedEvidence):
                    raise EvidenceStorageError("evidence.storage.resolution-mismatch")
                identity = item.reference.identity
                if (
                    identity in received
                    or pending.get(identity) != item.reference
                    or located.get((identity, item.locator.store_id)) != item.locator
                ):
                    raise EvidenceStorageError("evidence.storage.resolution-mismatch")
                # Verify the port result at this boundary; do not trust construction.
                received[identity] = ResolvedEvidence(
                    item.reference, item.content, item.locator
                )
            if set(received) != set(pending):
                raise EvidenceStorageError("evidence.storage.resolution-mismatch")
            self._objects.update(received)
            self._total_bytes += new_bytes
        return tuple(self._objects[key] for key in sorted(expected))


def resolve_statement_evidence(
    statement: EvidenceStatement,
    resolver: EvidenceResolver,
    *,
    locators: tuple[EvidenceLocator, ...],
) -> tuple[ResolvedEvidence, ...]:
    """Resolve direct blob references. Nested envelopes still require verification.

    This service does not authenticate the statement, recurse into referenced runs,
    evaluate retention or promote a receipt. The admission service owns that work.
    """

    predicate = statement.predicate
    references = [predicate.subject]
    if isinstance(predicate, DerivationRun):
        references.extend(item.blob for item in predicate.inputs)
        references.append(predicate.journal)
    elif isinstance(predicate, PlatformRun):
        references.extend((predicate.derivation, predicate.environment))
        references.extend(item.blob for item in predicate.checks)
    elif isinstance(predicate, EvidenceMatrix):
        references.extend(item.blob for item in predicate.cells)
    return resolver.resolve_many(tuple(references), locators=locators)
