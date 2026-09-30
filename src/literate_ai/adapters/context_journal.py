"""Durable CAS adapter for privacy-safe forward-generation context evidence."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from literate_ai.application.component_generation_preparation import (
    PreparedComponentGenerationNode,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.executable_components.context_journal import (
    ComponentContextBenchmarkRecord,
    ContextCacheOutcome,
    ForwardGenerationContextCacheEntry,
    ForwardGenerationContextCacheReport,
    ForwardGenerationPromptJournal,
    create_component_context_benchmark_record,
    create_forward_generation_context_cache_entry,
    create_forward_generation_prompt_journal,
)
from literate_ai.contracts.executable_components.source_generation import (
    SourceGenerationDisposition,
    SourceGenerationNodeResult,
)
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.storage import FileSystemCAS


class ForwardGenerationContextJournalError(RuntimeError):
    """Durable context evidence is missing, changed, or has the wrong type."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


_Record = TypeVar(
    "_Record",
    ForwardGenerationPromptJournal,
    ComponentContextBenchmarkRecord,
    ForwardGenerationContextCacheReport,
)


class FilesystemForwardGenerationContextJournal:
    """Persist exact journal, benchmark, and cache records without prompt text."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise TypeError("context journal root must be a Path")
        self.cas = FileSystemCAS(root)

    def put_prompt_journal(self, value: ForwardGenerationPromptJournal) -> BlobRef:
        return self._put(
            value,
            expected_type=ForwardGenerationPromptJournal,
            media_type="application/vnd.literate-ai.forward-generation-journal+json",
        )

    def get_prompt_journal(self, reference: BlobRef) -> ForwardGenerationPromptJournal:
        return self._get(
            reference,
            parser=ForwardGenerationPromptJournal.from_dict,
            expected_type=ForwardGenerationPromptJournal,
        )

    def put_benchmark_record(self, value: ComponentContextBenchmarkRecord) -> BlobRef:
        return self._put(
            value,
            expected_type=ComponentContextBenchmarkRecord,
            media_type="application/vnd.literate-ai.component-context-benchmark+json",
        )

    def get_benchmark_record(
        self, reference: BlobRef
    ) -> ComponentContextBenchmarkRecord:
        return self._get(
            reference,
            parser=ComponentContextBenchmarkRecord.from_dict,
            expected_type=ComponentContextBenchmarkRecord,
        )

    def put_cache_report(self, value: ForwardGenerationContextCacheReport) -> BlobRef:
        return self._put(
            value,
            expected_type=ForwardGenerationContextCacheReport,
            media_type="application/vnd.literate-ai.context-cache-report+json",
        )

    def get_cache_report(
        self, reference: BlobRef
    ) -> ForwardGenerationContextCacheReport:
        return self._get(
            reference,
            parser=ForwardGenerationContextCacheReport.from_dict,
            expected_type=ForwardGenerationContextCacheReport,
        )

    def _put(
        self,
        value: _Record,
        *,
        expected_type: type[_Record],
        media_type: str,
    ) -> BlobRef:
        if not isinstance(value, expected_type):
            raise TypeError(f"record must be a {expected_type.__name__}")
        reference = self.cas.put_manifest(value.to_dict(), media_type=media_type)
        if reference.digest != value.identity.digest:
            raise ForwardGenerationContextJournalError(
                "context_journal.identity_mismatch",
                "durable record bytes differ from the typed record identity",
            )
        return reference

    def _get(
        self,
        reference: BlobRef,
        *,
        parser,
        expected_type: type[_Record],
    ) -> _Record:
        if not isinstance(reference, BlobRef):
            raise TypeError("context journal reference must be a BlobRef")
        try:
            value = parser(self.cas.get_manifest(reference))
        except Exception as exc:
            raise ForwardGenerationContextJournalError(
                "context_journal.record_invalid",
                "durable context record is unavailable or invalid",
            ) from exc
        if (
            not isinstance(value, expected_type)
            or value.identity.digest != reference.digest
        ):
            raise ForwardGenerationContextJournalError(
                "context_journal.identity_mismatch",
                "durable context record differs from its content address",
            )
        return value


@dataclass(frozen=True, slots=True)
class RecordedForwardGenerationContextEvidence:
    journal: ForwardGenerationPromptJournal
    journal_reference: BlobRef
    benchmark: ComponentContextBenchmarkRecord
    benchmark_reference: BlobRef
    cache_entry: ForwardGenerationContextCacheEntry


class FilesystemForwardGenerationContextEvidenceRecorder:
    """Project and persist context evidence at the exact per-node result seam."""

    def __init__(
        self,
        journal: FilesystemForwardGenerationContextJournal,
        *,
        cache_key: Callable[
            [PreparedComponentGenerationNode[object, object]], ContentIdentity
        ],
    ) -> None:
        if not isinstance(journal, FilesystemForwardGenerationContextJournal):
            raise TypeError(
                "journal must be a FilesystemForwardGenerationContextJournal"
            )
        if not callable(cache_key):
            raise TypeError("cache_key must be callable")
        self.journal = journal
        self.cache_key = cache_key
        self._records: dict[str, RecordedForwardGenerationContextEvidence] = {}
        self._lock = threading.RLock()

    def start_attempt(self) -> None:
        """Begin one lifecycle projection while retaining prior CAS evidence."""

        with self._lock:
            self._records.clear()

    def record(
        self,
        prepared: PreparedComponentGenerationNode[object, object],
        result: SourceGenerationNodeResult,
    ) -> None:
        if not isinstance(prepared, PreparedComponentGenerationNode):
            raise TypeError("prepared must be a PreparedComponentGenerationNode")
        if not isinstance(result, SourceGenerationNodeResult):
            raise TypeError("result must be a SourceGenerationNodeResult")
        prompt_journal = create_forward_generation_prompt_journal(
            prepared.request.request, result
        )
        benchmark = create_component_context_benchmark_record(prompt_journal, result)
        key = self.cache_key(prepared)
        if not isinstance(key, ContentIdentity):
            raise TypeError("context cache-key provider must return a ContentIdentity")
        outcome = {
            SourceGenerationDisposition.REUSED: ContextCacheOutcome.HIT,
            SourceGenerationDisposition.GENERATED: ContextCacheOutcome.MISS,
            SourceGenerationDisposition.RETAINED: ContextCacheOutcome.BYPASS,
            SourceGenerationDisposition.FAILED: ContextCacheOutcome.BYPASS,
            SourceGenerationDisposition.CANCELLED: ContextCacheOutcome.BYPASS,
        }[result.disposition]
        cache_entry = create_forward_generation_context_cache_entry(
            prompt_journal, cache_key_identity=key, outcome=outcome
        )
        journal_reference = self.journal.put_prompt_journal(prompt_journal)
        benchmark_reference = self.journal.put_benchmark_record(benchmark)
        record = RecordedForwardGenerationContextEvidence(
            prompt_journal,
            journal_reference,
            benchmark,
            benchmark_reference,
            cache_entry,
        )
        uri = result.component_revision.uri
        with self._lock:
            previous = self._records.get(uri)
            if previous is not None and previous != record:
                raise ForwardGenerationContextJournalError(
                    "context_journal.component_result_changed",
                    "a Component context result changed within one lifecycle",
                )
            self._records[uri] = record

    @property
    def records(self) -> tuple[RecordedForwardGenerationContextEvidence, ...]:
        with self._lock:
            return tuple(self._records[uri] for uri in sorted(self._records))

    @property
    def prompt_journal_identities(self) -> tuple[ContentIdentity, ...]:
        return tuple(item.journal.identity for item in self.records)

    @property
    def benchmark_records(self) -> tuple[ComponentContextBenchmarkRecord, ...]:
        return tuple(item.benchmark for item in self.records)

    def cache_report(self) -> ForwardGenerationContextCacheReport:
        report = ForwardGenerationContextCacheReport(
            tuple(item.cache_entry for item in self.records)
        )
        self.journal.put_cache_report(report)
        return report


__all__ = [
    "FilesystemForwardGenerationContextJournal",
    "FilesystemForwardGenerationContextEvidenceRecorder",
    "ForwardGenerationContextJournalError",
    "RecordedForwardGenerationContextEvidence",
]
