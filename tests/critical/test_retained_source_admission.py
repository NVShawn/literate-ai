"""Retained admission binds its reviewed origin without inventing model evidence."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.cache import FileSystemSourceCache
from literate_ai.adapters.cache.standard import (
    StandardSourceCacheRestorationError,
    accepted_retained_source_review,
    restore_standard_source_cache_membership,
)
from literate_ai.application.standard_source_admission import (
    StandardSourceAdmissionError,
    StandardSourceAdmissionRequest,
    StandardSourceAdmissionService,
)
from literate_ai.contracts import (
    ContractValidationError,
    SourceCacheModelBinding,
    StandardSourceAdmissionMembership,
    canonical_identity,
)
from literate_ai.contracts.model_scopes import (
    RETAINED_SOURCE_NO_MODEL_SELECTOR,
    RETAINED_SOURCE_PROVIDER,
)
from literate_ai.storage import FileSystemCAS
from tests.critical import test_standard_source_admission as fixtures
from tests.critical.test_retained_source_cache_trust import _standard_entry


def retained_entry(cas):
    entry = _standard_entry(cas, retained=True)
    provenance = entry.standard_source_membership.generation.output.provenance
    review = provenance.retained_source_identity
    tool = canonical_identity(
        {
            "schema": "literate-ai/retained-source-selection@1",
            "origin": RETAINED_SOURCE_PROVIDER,
            "review_identity": review.uri,
            "generation_provider": None,
        }
    )
    key = replace(
        entry.derivation.cache_key,
        model_binding=SourceCacheModelBinding(
            RETAINED_SOURCE_PROVIDER, RETAINED_SOURCE_NO_MODEL_SELECTOR
        ),
        coding_cli_tool_binding_identity=tool,
    )
    return replace(entry, derivation=replace(entry.derivation, cache_key=key))


def admission_request(entry):
    generation = entry.standard_source_membership.generation
    key = entry.derivation.cache_key
    return StandardSourceAdmissionRequest(
        generation,
        key,
        fixtures.identity("flavors"),
        fixtures.identity("skills"),
        key.coding_cli_tool_binding_identity,
        None,
        fixtures.target_independent(),
        fixtures.identity("distribution"),
        retained_source_review_identity=generation.output.provenance.retained_source_identity,
    )


class RetainedSourceAdmissionTests(unittest.TestCase):
    def test_retained_and_transcript_origins_have_closed_distinct_wire_shapes(self):
        from jsonschema import Draft202012Validator, ValidationError
        from referencing import Registry, Resource

        from literate_ai.contracts import StandardSourceAdmissionEvidence
        from literate_ai.schema_catalog import schema_catalog_root

        documents = [
            json.loads(path.read_bytes())
            for version in ("v1", "v2")
            for path in schema_catalog_root(version).glob("*.schema.json")
        ]
        registry = Registry().with_resources(
            (document["$id"], Resource.from_contents(document))
            for document in documents
        )
        validator = Draft202012Validator(
            {"$ref": "urn:literate-ai:schema:v2:standard-source-admission-evidence"},
            registry=registry,
        )
        with tempfile.TemporaryDirectory() as directory:
            request = admission_request(
                retained_entry(FileSystemCAS(Path(directory).resolve() / "cas"))
            )
            evidence = (
                StandardSourceAdmissionService(fixtures.Verifier(request.generation))
                .prepare(request)
                .evidence
            )
        retained = evidence.to_dict()
        # Existing model-origin evidence keeps its original wire and identity.
        normal = replace(
            evidence,
            retained_source_review_identity=None,
            coding_cli_transcript_identity=fixtures.identity("transcript"),
        )
        self.assertNotIn("retained_source_review_identity", normal.to_dict())
        for valid in (retained, normal.to_dict()):
            validator.validate(valid)
            self.assertEqual(
                StandardSourceAdmissionEvidence.from_dict(valid).to_dict(), valid
            )
        neither = dict(retained)
        del neither["retained_source_review_identity"]
        for invalid in (
            neither,
            {
                **retained,
                "coding_cli_transcript_identity": fixtures.identity(
                    "transcript"
                ).to_dict(),
            },
            {**retained, "retained_source_review_identity": None},
        ):
            with self.assertRaises(ValidationError):
                validator.validate(invalid)
            with self.assertRaises((ContractValidationError, TypeError, ValueError)):
                StandardSourceAdmissionEvidence.from_dict(invalid)

    def test_verified_review_roundtrips_in_the_same_complete_cache_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cas = FileSystemCAS(root / "cas")
            entry = retained_entry(cas)
            request = admission_request(entry)
            verifier = fixtures.Verifier(request.generation)
            publisher = fixtures.Publisher()
            membership = StandardSourceAdmissionService(verifier, publisher).prepare(
                request
            )
            self.assertEqual(publisher.memberships, [])
            self.assertEqual(verifier.calls, 1)
            self.assertNotIn(
                "coding_cli_transcript_identity", membership.evidence.to_dict()
            )
            self.assertEqual(
                membership.evidence.retained_source_review_identity,
                request.retained_source_review_identity,
            )
            entry = replace(entry, source_admission_evidence=membership.evidence)
            cache = FileSystemSourceCache("retained", root / "cache")
            cache.publish(entry, caller_cas=cas)
            restored = cache.verified_published_entries()[0]
            self.assertEqual(restored, entry)
            self.assertIsInstance(
                restore_standard_source_cache_membership(restored),
                StandardSourceAdmissionMembership,
            )
            self.assertEqual(
                accepted_retained_source_review(
                    root / "cache", request.coding_cli_tool_binding_identity
                ),
                request.retained_source_review_identity,
            )
            for changed in (
                replace(
                    membership.evidence,
                    retained_source_review_identity=fixtures.identity("other-review"),
                ),
                replace(
                    membership.evidence,
                    coding_cli_tool_binding_identity=fixtures.identity("other-tool"),
                ),
            ):
                with self.assertRaises(ContractValidationError):
                    replace(entry, source_admission_evidence=changed)

    def test_full_cached_acceptance_without_source_admission_is_insufficient(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cas = FileSystemCAS(root / "cas")
            entry = retained_entry(cas)
            cache = FileSystemSourceCache("legacy", root / "cache")
            cache.publish(entry, caller_cas=cas)
            with self.assertRaises(StandardSourceCacheRestorationError):
                accepted_retained_source_review(
                    root / "cache",
                    entry.derivation.cache_key.coding_cli_tool_binding_identity,
                )
            with self.assertRaises(StandardSourceCacheRestorationError):
                accepted_retained_source_review(
                    root / "absent",
                    entry.derivation.cache_key.coding_cli_tool_binding_identity,
                )
            self.assertFalse((root / "absent").exists())

    def test_substituted_review_or_model_scope_refuses_before_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            request = admission_request(
                retained_entry(FileSystemCAS(Path(directory).resolve() / "cas"))
            )
            for changed in (
                replace(
                    request,
                    retained_source_review_identity=fixtures.identity("other-review"),
                ),
                replace(
                    request,
                    coding_cli_tool_binding_identity=fixtures.identity("other-tool"),
                ),
                replace(
                    request,
                    cache_key=replace(
                        request.cache_key,
                        source_semantics_identity=fixtures.identity(
                            "other-tree-review"
                        ),
                    ),
                ),
                replace(
                    request,
                    cache_key=replace(
                        request.cache_key,
                        model_binding=SourceCacheModelBinding("codex", "some-model"),
                    ),
                ),
            ):
                verifier = fixtures.Verifier(request.generation)
                with self.assertRaises(StandardSourceAdmissionError):
                    StandardSourceAdmissionService(verifier).prepare(changed)
                self.assertEqual(verifier.calls, 0)


if __name__ == "__main__":
    unittest.main()
