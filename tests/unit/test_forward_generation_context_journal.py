"""Durable privacy-safe context evidence for forward generation."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.context_journal import (
    FilesystemForwardGenerationContextJournal,
    ForwardGenerationContextJournalError,
)
from literate_ai.application.component_generation_context import (
    prepare_component_generation_context,
)
from literate_ai.contracts.executable_components.context import (
    ContextAuthorityKind,
    ContextVisibility,
)
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
from literate_ai.contracts.executable_components.scheduling import (
    ComponentGenerationRuntimeObservation,
)
from literate_ai.contracts.executable_components.source_generation import (
    SourceGenerationDisposition,
    SourceGenerationNodeResult,
)
from literate_ai.contracts.identity import canonical_identity
from tests.unit.test_component_generation_context import (
    _budget,
    _diamond_lock,
    _materialize,
    _named_plan,
)
from tests.unit.test_schema_catalog import SchemaCatalog


def _fixture():
    plan, inputs = _materialize(
        _named_plan(_diamond_lock(), "invoice-cli"),
        suffix="-PUBLIC-OR-LOCAL-CONTENT-CANARY",
    )
    prepared = prepare_component_generation_context(
        plan,
        framework_envelope=b"FRAMEWORK-PROMPT-INSTRUCTION-CANARY",
        authority_segments=tuple(
            replace(
                item,
                reason="SEGMENT-REASON-CANARY",
            )
            for item in inputs
        ),
        budget=_budget(),
    )
    request = prepared.request
    result = SourceGenerationNodeResult(
        component_revision=plan.component_revision,
        generation_plan_identity=plan.identity,
        generation_key_identity=plan.generation_key.identity,
        context_manifest_identity=request.context_manifest_identity,
        complexity_budget_identity=request.budget.identity,
        complexity_decision_identity=request.complexity_decision_identity,
        prompt_identity=request.prompt_identity,
        recipe_identity=canonical_identity({"recipe": "fixture"}),
        workspace_allocation_identity=canonical_identity({"workspace": "fixture"}),
        disposition=SourceGenerationDisposition.GENERATED,
        candidate_identity=canonical_identity({"candidate": "fixture"}),
        provenance_identity=canonical_identity({"provenance": "fixture"}),
        runtime_observation=ComponentGenerationRuntimeObservation(
            model_attempts=1,
            wall_time_ms=25,
            model_tokens=321,
            cost_microunits=17,
        ),
        failure_code=None,
    )
    return prepared, result


class ForwardGenerationContextJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prepared, self.result = _fixture()
        self.journal = create_forward_generation_prompt_journal(
            self.prepared.request, self.result
        )
        self.benchmark = create_component_context_benchmark_record(
            self.journal, self.result
        )
        self.schemas = SchemaCatalog()

    def test_journal_round_trips_every_exact_binding_without_prompt_content(
        self,
    ) -> None:
        wire = self.journal.to_dict()
        self.assertEqual(ForwardGenerationPromptJournal.from_dict(wire), self.journal)
        encoded = json.dumps(wire, sort_keys=True)

        self.assertNotIn("PROMPT-INSTRUCTION-CANARY", encoded)
        self.assertNotIn("SEGMENT-REASON-CANARY", encoded)
        self.assertNotIn("PUBLIC-OR-LOCAL-CONTENT-CANARY", encoded)
        self.assertNotIn("PRIVATE-TRANSITIVE-CANARY", encoded)
        self.assertNotIn("reason", encoded)
        self.assertNotIn('content":', encoded)
        request = self.prepared.request
        self.assertEqual(self.journal.bounded_request_identity, request.identity)
        self.assertEqual(
            self.journal.context_manifest_identity,
            request.context_manifest.identity,
        )
        self.assertEqual(
            self.journal.complexity_budget_identity, request.budget.identity
        )
        self.assertEqual(
            self.journal.complexity_decision_identity,
            request.budget_decision.identity,
        )
        self.assertEqual(self.journal.prompt_identity, request.prompt_identity)
        self.assertEqual(self.journal.node_result_identity, self.result.identity)
        self.assertEqual(len(self.journal.direct_interface_bindings), 2)

    def test_segment_projection_contains_only_allowed_manifest_authority(self) -> None:
        bindings = self.journal.segment_bindings
        self.assertEqual(
            tuple(item.index for item in bindings), tuple(range(len(bindings)))
        )
        self.assertFalse(
            any(item.visibility is ContextVisibility.FORBIDDEN for item in bindings)
        )
        direct = tuple(
            item
            for item in bindings
            if item.visibility is ContextVisibility.DIRECT_PUBLIC_INTERFACE
        )
        self.assertEqual(
            tuple(item.content_identity for item in direct),
            tuple(
                item.public_interface_identity
                for item in self.journal.direct_interface_bindings
            ),
        )
        with self.assertRaises(ValueError):
            replace(
                bindings[1],
                authority_kind=ContextAuthorityKind.PRIVATE_DEPENDENCY_SPECIFICATION,
                visibility=ContextVisibility.LOCAL_AUTHORITY,
            )

    def test_benchmark_record_reports_separate_overhead_and_zero_leakage(self) -> None:
        record = self.benchmark
        self.assertEqual(
            ComponentContextBenchmarkRecord.from_dict(record.to_dict()), record
        )
        self.assertEqual(record.actual_model_tokens, 321)
        self.assertEqual(
            record.context_manifest_identity,
            self.journal.context_manifest_identity,
        )
        self.assertEqual(
            record.complexity_budget_identity,
            self.journal.complexity_budget_identity,
        )
        self.assertEqual(
            record.complexity_decision_identity,
            self.journal.complexity_decision_identity,
        )
        self.assertEqual(record.private_transitive_leakage_bytes, 0)
        self.assertEqual(
            record.delivered_dependency_context_bytes,
            record.selected_direct_public_interface_bytes,
        )
        self.assertEqual(
            record.framework_context_bytes
            + record.local_authority_bytes
            + record.delivered_dependency_context_bytes,
            record.prompt_bytes,
        )
        self.assertEqual(
            record.prompt_bytes,
            self.prepared.request.budget_decision.prompt_bytes,
        )

    def test_cache_report_binds_context_and_interface_set_without_content(self) -> None:
        first = create_forward_generation_context_cache_entry(
            self.journal,
            cache_key_identity=canonical_identity({"cache": "invoice"}),
            outcome=ContextCacheOutcome.MISS,
        )
        second = replace(
            first,
            component_revision=canonical_identity({"component": "other"}),
            cache_key_identity=canonical_identity({"cache": "other"}),
            outcome=ContextCacheOutcome.HIT,
        )
        report = ForwardGenerationContextCacheReport(
            tuple(
                sorted(
                    (first, second),
                    key=lambda item: (
                        item.component_revision.uri,
                        item.cache_key_identity.uri,
                    ),
                )
            )
        )

        self.assertEqual(
            ForwardGenerationContextCacheReport.from_dict(report.to_dict()), report
        )
        self.assertEqual((report.hits, report.misses, report.bypasses), (1, 1, 0))
        encoded = json.dumps(report.to_dict(), sort_keys=True)
        self.assertNotIn("PROMPT-INSTRUCTION-CANARY", encoded)
        self.assertNotIn("PUBLIC-OR-LOCAL-CONTENT-CANARY", encoded)
        self.assertEqual(
            first.context_manifest_identity, self.journal.context_manifest_identity
        )
        self.assertEqual(
            first.complexity_budget_identity,
            self.journal.complexity_budget_identity,
        )
        self.assertIsInstance(
            ForwardGenerationContextCacheEntry.from_dict(first.to_dict()),
            ForwardGenerationContextCacheEntry,
        )

    def test_mismatched_result_cannot_be_journaled_or_benchmarked(self) -> None:
        changed = replace(
            self.result,
            context_manifest_identity=canonical_identity({"context": "substituted"}),
        )
        with self.assertRaises(ValueError):
            create_forward_generation_prompt_journal(self.prepared.request, changed)
        with self.assertRaises(ValueError):
            create_component_context_benchmark_record(self.journal, changed)

    def test_canonical_records_are_durable_and_mutation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = FilesystemForwardGenerationContextJournal(Path(temporary))
            journal_ref = store.put_prompt_journal(self.journal)
            benchmark_ref = store.put_benchmark_record(self.benchmark)
            cache_report = ForwardGenerationContextCacheReport(
                (
                    create_forward_generation_context_cache_entry(
                        self.journal,
                        cache_key_identity=canonical_identity({"cache": "fixture"}),
                        outcome=ContextCacheOutcome.MISS,
                    ),
                )
            )
            cache_ref = store.put_cache_report(cache_report)

            self.assertEqual(store.get_prompt_journal(journal_ref), self.journal)
            self.assertEqual(store.get_benchmark_record(benchmark_ref), self.benchmark)
            self.assertEqual(store.get_cache_report(cache_ref), cache_report)
            self.assertEqual(journal_ref.digest, self.journal.identity.digest)
            self.assertEqual(benchmark_ref.digest, self.benchmark.identity.digest)
            self.assertEqual(cache_ref.digest, cache_report.identity.digest)

            path = store.cas.path_for(journal_ref)
            path.write_bytes(path.read_bytes() + b"changed")
            with self.assertRaises(ForwardGenerationContextJournalError) as caught:
                store.get_prompt_journal(journal_ref)
            self.assertEqual(caught.exception.code, "context_journal.record_invalid")

    def test_cache_report_rejects_duplicate_entries_and_forged_counts(self) -> None:
        entry = create_forward_generation_context_cache_entry(
            self.journal,
            cache_key_identity=canonical_identity({"cache": "fixture"}),
            outcome=ContextCacheOutcome.BYPASS,
        )
        with self.assertRaises(ValueError):
            ForwardGenerationContextCacheReport((entry, entry))
        wire = ForwardGenerationContextCacheReport((entry,)).to_dict()
        wire["hits"] = 1
        with self.assertRaises(ValueError):
            ForwardGenerationContextCacheReport.from_dict(wire)

    def test_every_context_evidence_wire_document_is_schema_valid(self) -> None:
        entry = create_forward_generation_context_cache_entry(
            self.journal,
            cache_key_identity=canonical_identity({"cache": "fixture"}),
            outcome=ContextCacheOutcome.MISS,
        )
        report = ForwardGenerationContextCacheReport((entry,))
        documents = (
            *self.journal.segment_bindings,
            *self.journal.direct_interface_bindings,
            self.journal,
            self.benchmark,
            entry,
            report,
        )
        for document in documents:
            with self.subTest(schema=document.SCHEMA):
                self.schemas.validate(document.SCHEMA, document.to_dict())

    def test_benchmark_wire_requires_exact_context_binding_fields(self) -> None:
        for name in (
            "context_manifest_identity",
            "complexity_budget_identity",
            "complexity_decision_identity",
        ):
            wire = self.benchmark.to_dict()
            del wire[name]
            with self.subTest(field=name):
                with self.assertRaises((AssertionError, ValueError)):
                    self.schemas.validate(self.benchmark.SCHEMA, wire)
                with self.assertRaises(ValueError):
                    ComponentContextBenchmarkRecord.from_dict(wire)


if __name__ == "__main__":
    unittest.main()
