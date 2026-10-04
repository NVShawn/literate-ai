"""A real retained receipt, not a hand-authored stage, admits signed drafting."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.repository_lineage import FilesystemRepositoryLineageStore
from literate_ai.contracts import RepositoryParentSelection
from tests.support.fixtures_test_cli_arbitrary_source import KEY, invoke, reviewed_static_graph
from tests.support.fixtures_test_retained_harness_receipts import (
    _adapter,
    _legacy_project,
    _selectors,
)


class AdoptedPromotionJourneyTests(unittest.TestCase):
    def test_retained_harness_receipt_then_signed_static_drafting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            adopted = root / "adopted"
            _legacy_project(adopted)
            (adopted / "app.py").write_text("def value():\n    return 7\n")
            _adapter().initialize(
                adopted,
                flavor_selectors=_selectors(adopted),
                source_intelligence_provider="none",
                convert=True,
            )

            def success(*arguments):
                status, output, errors = invoke(*arguments)
                self.assertEqual((status, errors), (0, ""), output or errors)
                return json.loads(output)["result"]

            # The gate must reject advancing before the retained harness has run.
            status, _output, _errors = invoke(
                "project",
                "convert-stage",
                "advance",
                "--to",
                "retained",
                "--project",
                str(adopted),
            )
            self.assertEqual(status, 2)
            candidate = root / "candidate.json"
            retained = success(
                "project",
                "test-receipt",
                "run-retained",
                str(candidate),
                "--project",
                str(adopted),
                "--worker-id",
                "local",
            )
            self.assertFalse(retained["native_component_acceptance"])
            success(
                "project",
                "test-receipt",
                "update",
                str(candidate),
                "--project",
                str(adopted),
            )
            stage = success(
                "project",
                "convert-stage",
                "advance",
                "--to",
                "retained",
                "--project",
                str(adopted),
            )
            self.assertEqual(stage["stage"], "retained")

            source = adopted / "components/legacy-project-wrapper/implementation"
            original = {
                path.relative_to(source): path.read_bytes()
                for path in source.rglob("*")
                if path.is_file()
            }
            key = root / "trust.key"
            key.write_bytes(KEY)
            attestation = success(
                "spec",
                "attest",
                str(source),
                "--signer",
                "reviewer",
                "--machine",
                "fixture",
                "--key",
                str(key),
            )
            attestation_path = root / "attestation.json"
            attestation_path.write_text(json.dumps(attestation))
            bundle = success(
                "spec",
                "derive",
                str(source),
                "--attestation",
                str(attestation_path),
                "--trust-key",
                str(key),
            )
            bundle_path = root / "bundle.json"
            bundle_path.write_text(json.dumps(bundle))
            graph_path = root / "graph.json"
            graph_path.write_text(json.dumps(reviewed_static_graph(bundle)))
            review_arguments = [
                "spec",
                "review",
                str(bundle_path),
                "--actor",
                "reviewer",
                "--key",
                str(key),
                "--component-graph",
                str(graph_path),
            ]
            for uncertainty in bundle["review_gate"]["blocking_uncertainty_ids"]:
                review_arguments.extend(("--resolve", uncertainty))
            review = success(*review_arguments)
            review_path = root / "review.json"
            review_path.write_text(json.dumps(review))
            accepted = success(
                "spec",
                "accept",
                str(source),
                str(bundle_path),
                "--review",
                str(review_path),
                "--target",
                str(root / "accepted"),
                "--integrate-project",
                str(adopted),
                "--trust-key",
                str(key),
            )
            self.assertTrue(accepted["qualification_required"])
            promoted_selection, _ = FilesystemRepositoryLineageStore(
                Path(accepted["promoted_project"]["project_target"])
            ).load()
            self.assertEqual(promoted_selection, RepositoryParentSelection.root())
            stage = success(
                "project",
                "convert-stage",
                "advance",
                "--to",
                "drafted",
                "--project",
                str(adopted),
            )
            self.assertEqual(stage["stage"], "drafted")
            self.assertEqual(stage["release_authority"], "original-source")
            self.assertEqual(
                {
                    path.relative_to(source): path.read_bytes()
                    for path in source.rglob("*")
                    if path.is_file()
                },
                original,
            )
