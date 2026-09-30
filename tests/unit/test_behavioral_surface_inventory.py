from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from literate_ai.source_to_specification import (
    BehavioralInterfaceKind,
    BehavioralSurfaceDisposition,
    BehavioralSurfaceInventory,
    BehavioralSurfaceInventoryItem,
    BehavioralSurfaceRequirement,
    EvidenceReference,
    SourceToSpecificationError,
    canonical_digest,
    collect_behavioral_surface_inventory,
    observation_facet_maps_interface,
    reconcile_behavioral_surface_inventory,
)


def evidence(name: str, *, symbol: str = "serve") -> EvidenceReference:
    return EvidenceReference(
        f"evidence:{name}",
        canonical_digest("source"),
        canonical_digest(name),
        "src/app.py",
        symbol,
    )


class BehavioralSurfaceInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.collector = canonical_digest("deterministic-intelligence-collector@1")
        self.translator = canonical_digest("coding-cli-translator@1")
        self.surface = BehavioralSurfaceInventoryItem.create(
            detector_identity=self.collector,
            language="python",
            interface_kind=BehavioralInterfaceKind.ENTRYPOINT,
            path="src/app.py",
            symbol="serve",
            evidence=(evidence("body"), evidence("signature")),
            requirement=BehavioralSurfaceRequirement.REQUIRED,
        )

    def test_stable_identity_excludes_translator_disposition(self) -> None:
        mapped = self.surface.mapped(("observation:b", "observation:a"))
        excluded = self.surface.excluded(
            reason="Reviewed non-public debug entrypoint.",
            review_identity=canonical_digest("review"),
        )

        self.assertEqual(mapped.surface_id, self.surface.surface_id)
        self.assertEqual(excluded.surface_id, self.surface.surface_id)
        self.assertEqual(
            mapped.mapped_observation_ids,
            ("observation:a", "observation:b"),
        )

    def test_shared_evidence_does_not_map_an_unrelated_interface_kind(self) -> None:
        self.assertTrue(
            observation_facet_maps_interface(
                "entrypoints", BehavioralInterfaceKind.ENTRYPOINT
            )
        )
        self.assertFalse(
            observation_facet_maps_interface(
                "entrypoints", BehavioralInterfaceKind.DEPENDENCY_EDGE
            )
        )

    def test_inventory_round_trips_and_blocks_required_unmapped_surface(self) -> None:
        inventory = BehavioralSurfaceInventory(
            canonical_digest("source"), self.collector, self.translator, (self.surface,)
        )
        encoded = json.loads(json.dumps(inventory.to_dict()))

        self.assertEqual(BehavioralSurfaceInventory.from_dict(encoded), inventory)
        self.assertEqual(inventory.blocking_surface_ids, (self.surface.surface_id,))
        with self.assertRaisesRegex(
            SourceToSpecificationError, "required behavioral surfaces remain unmapped"
        ):
            inventory.require_complete()

        complete = BehavioralSurfaceInventory(
            inventory.source_snapshot_identity,
            inventory.collector_identity,
            inventory.translator_identity,
            (self.surface.mapped(("observation:serve",)),),
        )
        complete.require_complete()

    def test_inventory_requires_canonical_order_and_independent_providers(self) -> None:
        second = BehavioralSurfaceInventoryItem.create(
            detector_identity=self.collector,
            language="python",
            interface_kind=BehavioralInterfaceKind.ERROR,
            path="src/app.py",
            symbol="InvalidRequest",
            evidence=(evidence("error", symbol="InvalidRequest"),),
            requirement=BehavioralSurfaceRequirement.REQUIRED,
        )
        descending = tuple(
            sorted(
                (self.surface, second), key=lambda item: item.surface_id, reverse=True
            )
        )
        with self.assertRaisesRegex(SourceToSpecificationError, "canonically ordered"):
            BehavioralSurfaceInventory(
                canonical_digest("source"),
                self.collector,
                self.translator,
                descending,
            )
        with self.assertRaisesRegex(SourceToSpecificationError, "must be distinct"):
            BehavioralSurfaceInventory(
                canonical_digest("source"),
                self.collector,
                self.collector,
                (self.surface,),
            )

    def test_exclusion_requires_review_and_tamper_is_rejected(self) -> None:
        with self.assertRaisesRegex(SourceToSpecificationError, "reviewed reason"):
            BehavioralSurfaceInventoryItem(
                self.surface.surface_id,
                self.surface.detector_identity,
                self.surface.language,
                self.surface.interface_kind,
                self.surface.path,
                self.surface.symbol,
                self.surface.evidence,
                self.surface.requirement,
                BehavioralSurfaceDisposition.EXCLUDED,
            )

        value = self.surface.to_dict()
        value["symbol"] = "different"
        with self.assertRaisesRegex(SourceToSpecificationError, "does not bind"):
            BehavioralSurfaceInventoryItem.from_dict(value)

    def test_pretranslation_collector_is_deterministic_and_detects_dependency_edges(
        self,
    ) -> None:
        main = evidence("main", symbol="main")
        error = evidence("error", symbol="InvalidRequestError")
        intelligence = SimpleNamespace(
            authority_source_snapshot_id=canonical_digest("source"),
            provider_id="fixture-intelligence",
            provider_version="1.1.1",
            executable_identity=canonical_digest("fixture-intelligence executable"),
            evidence=(
                SimpleNamespace(
                    reference=error,
                    language="python",
                    kind="symbol",
                    content="class InvalidRequestError(Exception): pass",
                ),
                SimpleNamespace(
                    reference=main,
                    language="python",
                    kind="symbol",
                    content="def main(): return client.request()",
                ),
            ),
            relationships=(
                SimpleNamespace(
                    kind="imports",
                    source_path=main.path,
                    source_symbol=main.symbol,
                    source_language="python",
                    target_symbol="client",
                ),
            ),
        )

        first = collect_behavioral_surface_inventory(intelligence)
        intelligence.evidence = tuple(reversed(intelligence.evidence))
        second = collect_behavioral_surface_inventory(intelligence)

        self.assertEqual(first, second)
        self.assertEqual(first.identity, second.identity)
        self.assertEqual(
            {item.interface_kind for item in first.surfaces},
            {
                BehavioralInterfaceKind.ENTRYPOINT,
                BehavioralInterfaceKind.ERROR,
                BehavioralInterfaceKind.DEPENDENCY_EDGE,
            },
        )
        self.assertTrue(
            all(
                item.detector_identity == first.collector_identity
                for item in first.surfaces
            )
        )

    def test_manifest_collector_recovers_entrypoints_exports_and_dependencies(
        self,
    ) -> None:
        package_content = json.dumps(
            {
                "bin": {"ledger": "cli.js"},
                "exports": {".": "index.js"},
                "dependencies": {"zod": "^4.0.0"},
            },
            sort_keys=True,
        )
        reference = EvidenceReference(
            "evidence:package",
            canonical_digest("source"),
            canonical_digest(package_content),
            "package.json",
            "",
        )
        intelligence = SimpleNamespace(
            authority_source_snapshot_id=canonical_digest("source"),
            provider_id="fixture-intelligence",
            provider_version="1.1.1",
            executable_identity=canonical_digest("fixture-intelligence executable"),
            evidence=(
                SimpleNamespace(
                    reference=reference,
                    language="javascript",
                    kind="source-file",
                    content=package_content,
                ),
            ),
            relationships=(),
        )

        inventory = collect_behavioral_surface_inventory(intelligence)

        self.assertEqual(
            {(item.interface_kind, item.symbol) for item in inventory.surfaces},
            {
                (BehavioralInterfaceKind.ENTRYPOINT, "bin:ledger"),
                (BehavioralInterfaceKind.EXPORT, "package-exports"),
                (BehavioralInterfaceKind.DEPENDENCY_EDGE, "dependency:zod"),
            },
        )

    def test_test_exports_remain_evidence_not_required_product_surfaces(self) -> None:
        cases = (
            ("python", "tests/litai_test.py"),
            ("python", "package/test_behavior.py"),
            ("javascript", "src/ledger.test.js"),
            ("typescript", "src/ledger.spec.ts"),
            ("rust", "tests/ledger.rs"),
            ("cpp", "src/ledger_test.cc"),
        )
        evidence_items = []
        for index, (language, path) in enumerate(cases):
            reference = EvidenceReference(
                f"evidence:test:{index}",
                canonical_digest("source"),
                canonical_digest(path),
                path,
                f"test_export_{index}",
            )
            evidence_items.append(
                SimpleNamespace(
                    reference=reference,
                    language=language,
                    kind="symbol",
                    content=f"test export {index}",
                )
            )
        product = EvidenceReference(
            "evidence:product",
            canonical_digest("source"),
            canonical_digest("product"),
            "src/application.py",
            "main",
        )
        evidence_items.append(
            SimpleNamespace(
                reference=product,
                language="python",
                kind="symbol",
                content="def main(): return 0",
            )
        )
        intelligence = SimpleNamespace(
            authority_source_snapshot_id=canonical_digest("source"),
            provider_id="fixture-intelligence",
            provider_version="1.1.1",
            executable_identity=canonical_digest("fixture-intelligence executable"),
            evidence=tuple(evidence_items),
            relationships=(),
        )

        inventory = collect_behavioral_surface_inventory(intelligence)

        self.assertEqual(
            {(item.path, item.symbol) for item in inventory.surfaces},
            {("src/application.py", "main")},
        )

    def test_collector_requires_distinct_ordering_and_tie_break_surfaces(self) -> None:
        main = evidence("ordered-main", symbol="main")
        content = (
            "def main(values: dict[str, int]):\n"
            "    lines = [key for key in sorted(values)]\n"
            "    winner = min(lines, key=lambda key: (-values[key], key))\n"
            "    return {'lines': lines, 'winner': winner}\n"
        )
        intelligence = SimpleNamespace(
            authority_source_snapshot_id=canonical_digest("ordered-source"),
            provider_id="fixture-intelligence",
            provider_version="1.1.1",
            executable_identity=canonical_digest("fixture-intelligence executable"),
            evidence=(
                SimpleNamespace(
                    reference=main,
                    language="python",
                    kind="symbol",
                    content=content,
                ),
            ),
            relationships=(),
        )

        inventory = collect_behavioral_surface_inventory(intelligence)
        ordering = tuple(
            item
            for item in inventory.surfaces
            if item.interface_kind is BehavioralInterfaceKind.ORDERING
        )

        self.assertEqual(
            {item.symbol for item in ordering},
            {
                "main#ordering:sequence-sort",
                "main#ordering:selection-tie-break",
            },
        )
        self.assertTrue(
            observation_facet_maps_interface(
                "runtime-semantics", BehavioralInterfaceKind.ORDERING
            )
        )

    def test_collector_requires_distinct_normalization_surfaces(self) -> None:
        main = evidence("normalized-main", symbol="main")
        content = (
            "import re\n"
            "def main(payload):\n"
            "    warehouse = payload['warehouse'].strip()\n"
            "    key = re.sub(r'[^a-z0-9]+', '-', warehouse.lower()).strip('-')\n"
            "    return {'warehouse': warehouse, 'key': key}\n"
        )
        intelligence = SimpleNamespace(
            authority_source_snapshot_id=canonical_digest("normalized-source"),
            provider_id="fixture-intelligence",
            provider_version="1.1.1",
            executable_identity=canonical_digest("fixture-intelligence executable"),
            evidence=(
                SimpleNamespace(
                    reference=main,
                    language="python",
                    kind="symbol",
                    content=content,
                ),
            ),
            relationships=(),
        )

        inventory = collect_behavioral_surface_inventory(intelligence)
        normalization = tuple(
            item
            for item in inventory.surfaces
            if item.interface_kind is BehavioralInterfaceKind.NORMALIZATION
        )

        self.assertEqual(
            {item.symbol for item in normalization},
            {
                "main#normalization:trim",
                "main#normalization:case-fold",
                "main#normalization:pattern-rewrite",
            },
        )
        self.assertTrue(
            observation_facet_maps_interface(
                "io-protocol", BehavioralInterfaceKind.NORMALIZATION
            )
        )

    def test_reconciliation_merges_verifier_and_advisory_model_surfaces(self) -> None:
        detected = BehavioralSurfaceInventory(
            canonical_digest("source"), self.collector, None, (self.surface,)
        )
        verifier_identity = canonical_digest("independent verifier")
        verifier_surface = BehavioralSurfaceInventoryItem.create(
            detector_identity=verifier_identity,
            language="python",
            interface_kind=BehavioralInterfaceKind.ERROR,
            path="src/app.py",
            symbol="InvalidRequestError",
            evidence=(evidence("error", symbol="InvalidRequestError"),),
            requirement=BehavioralSurfaceRequirement.REQUIRED,
        )
        proposal = BehavioralSurfaceInventoryItem.create(
            detector_identity=self.translator,
            language="python",
            interface_kind=BehavioralInterfaceKind.STATE,
            path="src/app.py",
            symbol="candidate-state",
            evidence=(evidence("state", symbol="candidate-state"),),
            requirement=BehavioralSurfaceRequirement.ADVISORY,
        )

        merged = reconcile_behavioral_surface_inventory(
            detected,
            translator_identity=self.translator,
            observations={
                "observation:entrypoint": (
                    "entrypoints",
                    tuple(item.evidence_id for item in self.surface.evidence),
                ),
                "observation:error": (
                    "errors",
                    tuple(item.evidence_id for item in verifier_surface.evidence),
                ),
            },
            verifier_surfaces=(verifier_surface,),
            model_proposals=(proposal,),
        )

        by_id = {item.surface_id: item for item in merged.surfaces}
        self.assertEqual(
            by_id[self.surface.surface_id].disposition,
            BehavioralSurfaceDisposition.MAPPED,
        )
        self.assertEqual(
            by_id[verifier_surface.surface_id].mapped_observation_ids,
            ("observation:error",),
        )
        self.assertEqual(
            by_id[proposal.surface_id].disposition,
            BehavioralSurfaceDisposition.UNMAPPED,
        )
        self.assertFalse(merged.blocking_surface_ids)
        self.assertNotEqual(merged.collector_identity, self.collector)

    def test_reconciliation_allows_only_reviewed_known_exclusions(self) -> None:
        detected = BehavioralSurfaceInventory(
            canonical_digest("source"), self.collector, None, (self.surface,)
        )
        review = canonical_digest("review")
        merged = reconcile_behavioral_surface_inventory(
            detected,
            translator_identity=self.translator,
            observations={},
            reviewed_exclusions={
                self.surface.surface_id: ("Reviewed private debug command.", review)
            },
        )
        merged.require_complete()
        self.assertEqual(
            merged.surfaces[0].disposition, BehavioralSurfaceDisposition.EXCLUDED
        )
        with self.assertRaisesRegex(SourceToSpecificationError, "unknown surface"):
            reconcile_behavioral_surface_inventory(
                detected,
                translator_identity=self.translator,
                observations={},
                reviewed_exclusions={
                    canonical_digest("unknown"): ("No such surface.", review)
                },
            )


if __name__ == "__main__":
    unittest.main()
