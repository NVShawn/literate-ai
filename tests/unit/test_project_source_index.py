"""Bounded opt-in project source-intelligence policy tests."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.contracts import (
    ProjectSourceIntelligencePolicy,
    SourceIntelligenceArtifactPublication,
    SourceIntelligenceMode,
    SourceIntelligenceStage,
)
from literate_ai.project_source_index import (
    CodeGraphProjectSourceIntelligence,
    _CodeGraphCommandEngine,
    require_lifecycle_project_index,
)


def disabled_policy() -> ProjectSourceIntelligencePolicy:
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


def codegraph_policy(
    mode: SourceIntelligenceMode = SourceIntelligenceMode.REQUIRED,
    *,
    minimum_version: str = "1.1.1",
) -> ProjectSourceIntelligencePolicy:
    return ProjectSourceIntelligencePolicy(
        provider_id="codegraph-cli",
        command="codegraph",
        minimum_version=minimum_version,
        artifact_path=".codegraph/codegraph.db",
        stages=tuple((stage, mode) for stage in SourceIntelligenceStage),
        artifact_publication=SourceIntelligenceArtifactPublication.METADATA_ONLY,
    )


class ProjectSourceIntelligenceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("codegraph"), "codegraph CLI is not installed")
    def test_real_codegraph_sync_and_check_when_provider_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "sample.py").write_text(
                "def answer() -> int:\n    return 42\n", encoding="utf-8"
            )
            engine = _CodeGraphCommandEngine("codegraph", timeout_seconds=60)
            version, _identity = engine.preflight(root)
            provider = CodeGraphProjectSourceIntelligence(
                codegraph_policy(minimum_version=version),
                engine=engine,
            )

            synchronized = provider.sync(root)
            checked = provider.check(root)

        self.assertEqual(synchronized["state"], "current")
        self.assertEqual(checked["state"], "current")
        self.assertGreaterEqual(checked["file_count"], 1)

    def test_default_none_never_constructs_external_provider(self) -> None:
        with mock.patch(
            "literate_ai.project_source_index.CodeGraphProjectSourceIntelligence"
        ) as provider:
            report = require_lifecycle_project_index(
                Path("/project"), disabled_policy()
            )

        provider.assert_not_called()
        self.assertEqual(report["state"], "off")
        self.assertEqual(report["provider_id"], "none")
        self.assertEqual(report["file_count"], 0)
        self.assertEqual(report["node_count"], 0)
        self.assertEqual(report["edge_count"], 0)
