"""Source-intelligence selection without a product indexer."""

from __future__ import annotations

import unittest

from literate_ai.adapters.intelligence import (
    SourceIntelligenceError,
    generated_source_tree_identity,
    select_source_intelligence_provider,
)
from literate_ai.contracts import (
    ProjectSourceIntelligencePolicy,
    SourceIntelligenceArtifactPublication,
    SourceIntelligenceMode,
    SourceIntelligenceStage,
)


def _none_policy() -> ProjectSourceIntelligencePolicy:
    return ProjectSourceIntelligencePolicy(
        provider_id="none",
        command=None,
        minimum_version=None,
        artifact_path=None,
        stages=tuple(
            (stage, SourceIntelligenceMode.OFF) for stage in SourceIntelligenceStage
        ),
        artifact_publication=SourceIntelligenceArtifactPublication.METADATA_ONLY,
    )


def _unsupported_policy(
    mode: SourceIntelligenceMode,
) -> ProjectSourceIntelligencePolicy:
    return ProjectSourceIntelligencePolicy(
        provider_id="unsupported-indexer",
        command="unsupported-indexer",
        minimum_version="1.0.0",
        artifact_path=".source-intelligence/index.json",
        stages=tuple((stage, mode) for stage in SourceIntelligenceStage),
        artifact_publication=SourceIntelligenceArtifactPublication.METADATA_ONLY,
    )


class GeneratedSourceIndexTests(unittest.TestCase):
    def test_tree_identity_is_stable_for_exact_files(self) -> None:
        files = {"app.py": b"print('ok')\n", "readme.md": b"# app\n"}
        first = generated_source_tree_identity(files)
        second = generated_source_tree_identity(dict(reversed(files.items())))
        self.assertTrue(first.startswith("sha256:"))
        self.assertEqual(first, second)

    def test_tree_identity_rejects_reserved_index_paths(self) -> None:
        with self.assertRaises(SourceIntelligenceError):
            generated_source_tree_identity({".codegraph/db": b"not-source\n"})

    def test_none_provider_selects_off_without_an_implementation(self) -> None:
        selection = select_source_intelligence_provider(
            _none_policy(), SourceIntelligenceStage.SOURCE_GENERATION
        )
        self.assertIsNone(selection.provider)
        self.assertEqual(selection.mode, SourceIntelligenceMode.OFF)
        self.assertEqual(
            selection.status(current=False),
            {
                "schema": "literate-ai/source-intelligence-stage-status@1",
                "stage": "source-generation",
                "mode": "off",
                "state": "off",
                "provider_id": "none",
            },
        )

    def test_unsupported_required_provider_fails_closed(self) -> None:
        with self.assertRaises(SourceIntelligenceError) as caught:
            select_source_intelligence_provider(
                _unsupported_policy(SourceIntelligenceMode.REQUIRED),
                SourceIntelligenceStage.STRUCTURAL_REVIEW,
            )
        self.assertEqual(
            caught.exception.code, "source-intelligence.provider-unsupported"
        )

    def test_unsupported_preferred_provider_reports_unavailable(self) -> None:
        selection = select_source_intelligence_provider(
            _unsupported_policy(SourceIntelligenceMode.PREFERRED),
            SourceIntelligenceStage.STRUCTURAL_REVIEW,
        )
        self.assertIsNone(selection.provider)
        self.assertEqual(
            selection.unavailable_reason, "source-intelligence.provider-unsupported"
        )
