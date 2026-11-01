"""A real retained receipt, not a hand-authored stage, admits signed drafting."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.locked_generation_authority import (
    FilesystemLockedGenerationAuthorityReader,
    LockedGenerationAuthorityReaderError,
)
from literate_ai.adapters.repository_lineage import FilesystemRepositoryLineageStore
from literate_ai.contracts import RepositoryParentSelection
from tests.support.fixtures_test_cli_arbitrary_source import (
    KEY,
    invoke,
    reviewed_static_graph,
)
from tests.support.fixtures_test_retained_harness_receipts import (
    _adapter,
    _legacy_project,
    _selectors,
)


class AdoptedPromotionJourneyTests(unittest.TestCase):
    def test_retained_harness_receipt_then_signed_static_drafting(self):
        self._journey(expected_build="+flavor://literate-ai/build-make")

    def test_reviewed_cargo_replaces_template_and_parent_build_defaults(self):
        self._journey(
            qualification_flavors=(
                "+flavor://literate-ai/lang-rust",
                "+flavor://literate-ai/build-cargo",
            ),
            expected_build="+flavor://literate-ai/build-cargo",
        )

    @unittest.skipUnless(shutil.which("cargo"), "native Cargo is unavailable")
    def test_real_cargo_parent_inherits_build_without_bazel_subtraction(self):
        self._journey(
            parent_cargo=True,
            qualification_flavors=("+flavor://literate-ai/lang-rust",),
            expected_build="+flavor://literate-ai/build-cargo",
        )

    def test_two_explicit_build_systems_still_refuse(self):
        self._journey(
            qualification_flavors=(
                "+flavor://literate-ai/lang-rust",
                "+flavor://literate-ai/build-cargo",
                "+flavor://literate-ai/build-make",
            ),
            expected_error="component_lock.mutually_exclusive_flavors",
        )

    def _journey(
        self,
        *,
        qualification_flavors=(),
        expected_build=None,
        expected_error=None,
        parent_cargo=False,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            adopted = root / "adopted"
            _legacy_project(adopted)
            (adopted / "app.py").write_text("def value():\n    return 7\n")
            if parent_cargo:
                # The Make fixture would otherwise own the retained harness even
                # though Cargo owns the language/build defaults. Require real Cargo.
                (adopted / "Makefile").unlink()
                (adopted / "Cargo.toml").write_text(
                    '[package]\nname="promotion-fixture"\nversion="1.0.0"\nedition="2021"\n'
                )
                (adopted / "src").mkdir()
                (adopted / "src/lib.rs").write_text(
                    "pub fn value() -> u32 { 7 }\n"
                    "#[test] fn value_is_seven() { assert_eq!(value(), 7); }\n"
                )
                (adopted / "src/main.rs").write_text(
                    'fn main() { println!("{}", promotion_fixture::value()); }\n'
                )
                subprocess.run(
                    ["cargo", "generate-lockfile", "--offline"],
                    cwd=adopted,
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
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
            flavor_arguments = tuple(
                part
                for flavor in qualification_flavors
                for part in ("--flavor", flavor)
            )
            accept_arguments = (
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
                *flavor_arguments,
            )
            if expected_error is not None:
                status, output, errors = invoke(*accept_arguments)
                self.assertEqual(status, 2, output)
                self.assertEqual(json.loads(errors)["error"]["code"], expected_error)
                self.assertFalse((root / "accepted").exists())
                return
            accepted = success(*accept_arguments)
            child = Path(accepted["promoted_project"]["project_target"])
            defaults = json.loads((child / "literate.project.json").read_text())[
                "default_flavor_selectors"
            ]
            self.assertIn(expected_build, defaults)
            self.assertNotIn("+flavor://literate-ai/build-bazel", defaults)
            component = (child / accepted["promoted_project"]["component"]).parent
            success("lock", str(component), "--target", "host", "--check")
            # This is the exact current-lock admission reader used by qualify,
            # not the CLI --check path, which permits equivalent selectors.
            reader = FilesystemLockedGenerationAuthorityReader()
            reader.read(component, target_name="host").require_unchanged()
            if qualification_flavors:
                with self.assertRaises(LockedGenerationAuthorityReaderError) as noop:
                    reader.read(
                        component,
                        target_name="host",
                        flavor_selectors=(
                            qualification_flavors[0],
                            qualification_flavors[0],
                        ),
                    )
                self.assertEqual(
                    noop.exception.code, "component_lock.flavor_selector_noop"
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
