"""Exact accepted source-derivation cache semantics."""

from __future__ import annotations

import unittest
from dataclasses import replace

from literate_ai.contracts import (
    CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR,
    AcceptedSourceDerivation,
    AcceptedSourceLookupKey,
    ContractValidationError,
    CycloneDxBomBinding,
    CycloneDxLifecycle,
    SourceCacheModelBinding,
    SourceDerivationCacheKey,
    canonical_identity,
    reusable_source_derivations,
    source_cache_model_selector,
)


def identity(label: str):
    return canonical_identity({"label": label})


def cache_key(*, model: str = "model-a") -> SourceDerivationCacheKey:
    return SourceDerivationCacheKey(
        recipe_identity=identity("recipe"),
        execution_plan_identity=identity("execution-plan"),
        coding_cli_tool_binding_identity=identity("coding-cli-tool-binding"),
        model_binding=SourceCacheModelBinding("test-provider", model),
        request_identity=identity("request"),
        source_semantics_identity=identity("source-semantics"),
    )


def accepted(key: SourceDerivationCacheKey, suffix: str) -> AcceptedSourceDerivation:
    managed = identity(f"managed-sbom-graph-{suffix}")
    source_sbom = identity(f"source-sbom-{suffix}")
    resolved_sbom = identity(f"resolved-sbom-{suffix}")
    composition = identity(f"component-composition-{suffix}")
    root_ref = f"urn:test:{suffix}"
    source_binding = CycloneDxBomBinding(
        CycloneDxLifecycle.SOURCE,
        source_sbom,
        identity(f"source-graph-{suffix}"),
        managed,
        composition,
        None,
        root_ref,
        1,
        0,
    )
    resolved_binding = CycloneDxBomBinding(
        CycloneDxLifecycle.RESOLVED,
        resolved_sbom,
        identity(f"resolved-graph-{suffix}"),
        managed,
        composition,
        source_sbom,
        root_ref,
        1,
        0,
    )
    return AcceptedSourceDerivation(
        cache_key=key,
        component_lock_identity=composition,
        source_tree_identity=identity(f"source-{suffix}"),
        managed_sbom_graph_identity=managed,
        source_sbom_identity=source_sbom,
        resolved_sbom_identity=resolved_sbom,
        source_sbom_binding=source_binding,
        resolved_sbom_binding=resolved_binding,
        generated_test_suite_identity=identity(f"generated-tests-{suffix}"),
        build_evidence_identity=identity(f"build-{suffix}"),
        test_evidence_identity=identity(f"tests-{suffix}"),
        acceptance_identity=identity(f"acceptance-{suffix}"),
        provenance_identity=identity(f"provenance-{suffix}"),
    )


class GenerationCacheTests(unittest.TestCase):
    def test_only_model_omission_maps_to_the_reserved_portable_selector(self) -> None:
        self.assertEqual(
            source_cache_model_selector(None),
            CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR,
        )
        self.assertEqual(source_cache_model_selector("model-a"), "model-a")
        with self.assertRaisesRegex(ContractValidationError, "reserved for an omitted"):
            source_cache_model_selector(CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR)

    def test_default_model_selector_is_an_explicit_portable_binding(self) -> None:
        binding = SourceCacheModelBinding(
            "test-provider", CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR
        )

        self.assertEqual(binding.model_selector, "cli-configured-default")

    def test_contracts_round_trip_with_content_stable_identities(self) -> None:
        key = cache_key()
        record = accepted(key, "one")

        self.assertEqual(SourceDerivationCacheKey.from_dict(key.to_dict()), key)
        self.assertEqual(
            AcceptedSourceLookupKey.from_dict(key.accepted_source_lookup.to_dict()),
            key.accepted_source_lookup,
        )
        self.assertEqual(AcceptedSourceDerivation.from_dict(record.to_dict()), record)
        self.assertEqual(record.identity, accepted(key, "one").identity)

    def test_accepted_lookup_excludes_only_transaction_request_identity(self) -> None:
        original = cache_key()
        fresh_session = replace(original, request_identity=identity("fresh-session"))

        self.assertNotEqual(original.identity, fresh_session.identity)
        self.assertEqual(
            original.accepted_source_lookup_identity,
            fresh_session.accepted_source_lookup_identity,
        )
        self.assertEqual(
            reusable_source_derivations(
                fresh_session,
                (accepted(original, "one"),),
                force_regeneration=False,
            ),
            (accepted(original, "one"),),
        )

        for field, value in (
            ("recipe_identity", identity("other-recipe")),
            ("execution_plan_identity", identity("other-plan")),
            ("coding_cli_tool_binding_identity", identity("other-provider-tool")),
            ("model_binding", SourceCacheModelBinding("other-provider", "model-a")),
            ("source_semantics_identity", identity("other-source-semantics")),
        ):
            with self.subTest(field=field):
                changed = replace(original, **{field: value})
                self.assertNotEqual(
                    original.accepted_source_lookup_identity,
                    changed.accepted_source_lookup_identity,
                )
                self.assertEqual(
                    reusable_source_derivations(
                        changed,
                        (accepted(original, "one"),),
                        force_regeneration=False,
                    ),
                    (),
                )

    def test_accepted_derivation_requires_the_exact_component_lock(self) -> None:
        record = accepted(cache_key(), "locked")
        omitted = record.to_dict()
        omitted.pop("component_lock_identity")
        with self.assertRaises(ContractValidationError):
            AcceptedSourceDerivation.from_dict(omitted)

        legacy = dict(omitted)
        legacy["schema"] = "urn:literate-ai:schema:v1:accepted-source-derivation"
        with self.assertRaisesRegex(ContractValidationError, "migration identity"):
            AcceptedSourceDerivation.from_dict(legacy)
        self.assertEqual(
            AcceptedSourceDerivation.from_dict(
                legacy,
                legacy_component_lock_identity=record.component_lock_identity,
            ),
            record,
        )

        substituted = record.to_dict()
        substituted["component_lock_identity"] = identity(
            "another-component-lock"
        ).to_dict()
        with self.assertRaises(ContractValidationError):
            AcceptedSourceDerivation.from_dict(substituted)

    def test_exact_key_can_have_multiple_accepted_model_derivations(self) -> None:
        key = cache_key()
        records = (accepted(key, "two"), accepted(key, "one"))

        reusable = reusable_source_derivations(key, records, force_regeneration=False)

        self.assertEqual(set(reusable), set(records))
        self.assertEqual(
            tuple(item.identity.uri for item in reusable),
            tuple(sorted(item.identity.uri for item in records)),
        )

    def test_model_binding_drift_and_forced_regeneration_bypass_cache(self) -> None:
        original = cache_key(model="model-a")
        changed = cache_key(model="model-b")
        record = accepted(original, "one")

        self.assertEqual(
            reusable_source_derivations(changed, (record,), force_regeneration=False),
            (),
        )
        self.assertEqual(
            reusable_source_derivations(original, (record,), force_regeneration=True),
            (),
        )
        self.assertEqual(
            reusable_source_derivations(
                original,
                (object(),),  # type: ignore[arg-type]
                force_regeneration=True,
            ),
            (),
        )


if __name__ == "__main__":
    unittest.main()
