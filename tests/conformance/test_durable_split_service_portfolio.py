"""Lock and CycloneDX contracts for the ADR 0027 four-boundary portfolio."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.application.standard_project_services import (
    StandardProjectApplicationService,
)
from tests.conformance.support.durable_split_service import (
    ROLE_COORDINATES,
    resolve_durable_split_portfolio,
)
from tests.conformance.support.sample_runner import (
    _locked_standard_sample_model_identities,
    _prepare_standard_sample_project,
    _StandardSampleAuthoritySnapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE = REPO_ROOT / "samples" / "durable-split-service"


class DurableSplitServicePortfolioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Lock resolution is read-only and the result is immutable; resolve once.
        cls.portfolio = resolve_durable_split_portfolio(SAMPLE, platform="macos")

    def test_real_locks_and_cyclonedx_preserve_exact_four_boundary_union(self) -> None:
        portfolio = self.portfolio

        self.assertEqual(len(portfolio.frontend.lock.nodes), 3)
        self.assertEqual(len(portfolio.collector.lock.nodes), 2)
        self.assertEqual(
            {
                node.revision.coordinate.uri
                for lock in portfolio.locks
                for node in lock.nodes
            },
            set(ROLE_COORDINATES.values()),
        )
        cache_revisions = {
            node.revision.identity.uri
            for lock in portfolio.locks
            for node in lock.nodes
            if node.revision.coordinate.uri == ROLE_COORDINATES["cache"]
        }
        self.assertEqual(len(cache_revisions), 1)
        self.assertEqual(len(portfolio.managed_graphs), 2)
        self.assertEqual(len(portfolio.source_bom_identities), 2)

    def test_every_locked_node_closes_its_generation_skill_taxonomy(self) -> None:
        portfolio = self.portfolio
        with tempfile.TemporaryDirectory(prefix="litai-durable-skills-") as temporary:
            for index, root in enumerate((portfolio.frontend, portfolio.collector)):
                snapshot = _StandardSampleAuthoritySnapshot(
                    root.authority, root.catalog
                )
                models = _locked_standard_sample_model_identities(
                    snapshot, coding_cli="codex", pipeline_model="gpt-5.4"
                )
                execution = StandardProjectApplicationService.plan(
                    root.lock, model_identities=models
                )
                prepared = _prepare_standard_sample_project(
                    snapshot=snapshot,
                    execution_plan=execution,
                    source_root=Path(temporary) / str(index),
                    coding_cli="codex",
                    pipeline_model="gpt-5.4",
                )
                self.assertEqual(len(prepared.nodes), len(root.lock.nodes))


if __name__ == "__main__":
    unittest.main()
