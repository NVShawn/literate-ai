"""Recomposed Standard runtimes keep their bounded candidate-repair port.

`litai rebuild` attaches durable checkpoints and the source cache after assembling
the host runtime. Both recompositions once dropped the repair port, so a single
retryable generated-test rejection failed the build without a fresh replacement.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from unittest import mock

from literate_ai.adapters import standard_project
from literate_ai.adapters.cache import FileSystemSourceCache, SourceCacheResolver
from literate_ai.adapters.cache.standard_checkpoint import (
    FilesystemStandardLifecycleCheckpointStore,
)
from literate_ai.cli.rebuild import _repair_report
from literate_ai.contracts import (
    SourceCacheConfiguration,
    SourceCacheMode,
    SourceCacheRootKind,
    SourceCacheTarget,
)
from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.contracts.executable_components import (
    CandidateAttemptChainDisposition,
)
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.standard_lifecycle_checkpoint import (
    StandardLifecycleCheckpointOutcome,
)
from literate_ai.storage import FileSystemCAS
from tests.support import fixtures_test_standard_project_lifecycle as lifecycle


def _runtime(repair: object) -> standard_project.FilesystemStandardProjectRuntime:
    lifecycle = mock.Mock(candidate_repair_port=repair)
    values = {
        field.name: mock.Mock()
        for field in fields(standard_project.FilesystemStandardProjectRuntime)
    }
    values["application"] = mock.Mock(lifecycle=lifecycle)
    return standard_project.FilesystemStandardProjectRuntime(**values)


class RecomposedRuntimeCandidateRepairTests(unittest.TestCase):
    def test_checkpoint_and_source_cache_recomposition_keep_the_repair_port(
        self,
    ) -> None:
        repair = object()
        assembled = mock.Mock(name="application")
        with (
            tempfile.TemporaryDirectory() as raw,
            mock.patch.object(
                standard_project,
                "assemble_standard_project_application_service",
                return_value=assembled,
            ) as assemble,
            mock.patch.object(
                standard_project, "FilesystemStandardLifecycleCheckpointStore"
            ),
            mock.patch.object(
                standard_project, "FilesystemStandardAcceptedSourcePublisher"
            ),
            mock.patch.object(standard_project, "FilesystemStandardSourceRestorer"),
        ):
            root = Path(raw).resolve()
            standard_project.compose_filesystem_standard_lifecycle_checkpoints(
                _runtime(repair), checkpoint_root=root / "checkpoints"
            )
            target = SourceCacheTarget(
                target_id="repair",
                root_kind=SourceCacheRootKind.OPERATOR_BOUND,
                root_reference="repair-cache",
            )
            resolver = SourceCacheResolver(
                SourceCacheConfiguration(
                    mode=SourceCacheMode.READ_WRITE,
                    targets=(target,),
                    write_target_id=target.target_id,
                    require_unique=True,
                ),
                {target.target_id: FileSystemSourceCache("repair", root / "cache")},
            )
            standard_project.compose_filesystem_standard_source_cache(
                _runtime(repair),
                generator=mock.Mock(),
                resolver=resolver,
                caller_cas=FileSystemCAS(root / "cas"),
                indexer=mock.Mock(),
            )
        self.assertEqual(assemble.call_count, 2)
        for call in assemble.call_args_list:
            self.assertIs(call.kwargs["candidate_repair_port"], repair)


class _SourceTrees:
    def register(self, *args, **kwargs) -> None:
        pass


class _Journal:
    """Forward to the real store while keeping Money's boundary order."""

    def __init__(self, store, revision) -> None:
        self.store = store
        self.revision = revision
        self.events: list[str] = []

    def record(self, evidence) -> None:
        self.store.record(evidence)
        if evidence.component_revision == self.revision:
            self.events.append(f"{evidence.stage.value}:{evidence.outcome.value}")

    def start_replacement(self, execution_plan_identity, component_revision) -> None:
        self.store.start_replacement(execution_plan_identity, component_revision)
        if component_revision == self.revision:
            self.events.append("replacement")


class DurableCheckpointCandidateRepairTests(unittest.TestCase):
    """Repair against the real checkpoint store that `litai rebuild` composes."""

    def setUp(self) -> None:
        self.lock = lifecycle._diamond_lock(dependency_kind=DependencyKind.GENERATION)
        self.execution, requests = lifecycle._prepared_execution(self.lock)
        self.nodes = lifecycle._prepared_nodes(self.execution, requests)
        self.names = lifecycle._names(self.lock)
        self.money = next(
            ContentIdentity.parse_uri(revision)
            for revision, name in self.names.items()
            if name == "money"
        )

    def _run(self, rejected_attempts: int):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source = root / "source"
            source.mkdir()
            (source / "main.txt").write_text("candidate", encoding="utf-8")
            store = FilesystemStandardLifecycleCheckpointStore(
                root / "checkpoints",
                source_root=lambda identity: source,
                source_trees=_SourceTrees(),
            )
            store.start_attempt(self.execution, self.nodes)
            journal = _Journal(store, self.money)
            result = lifecycle._service(
                lifecycle.RepairableBuildPorts(
                    self.execution, self.names, rejected_attempts=rejected_attempts
                ),
                candidate_repair_port=lifecycle._RepairPort(),
                checkpoint_recorder=journal,
            ).execute(
                self.execution,
                component_lock=self.lock,
                invalidation=lifecycle._decision(
                    self.execution, self.names, "money", tuple(self.names.values())
                ),
                prepared_nodes=self.nodes,
                max_parallelism=2,
            )
            attempt = store.latest_attempt(self.execution.identity, self.money)
            head = store.latest(self.execution.identity, self.money)
        chain = next(
            item
            for item in result.candidate_attempt_chains
            if item.component_revision == self.money
        )
        self.assertEqual(journal.events.count("replacement"), len(chain.attempts) - 1)
        # Every rejected attempt is closed as failed before its replacement.
        for index, event in enumerate(journal.events):
            if event == "replacement":
                self.assertTrue(journal.events[index - 1].endswith(":failed"))
        return result, chain, attempt, head

    def test_a_replacement_opens_a_fresh_attempt_and_is_accepted(self) -> None:
        result, chain, attempt, head = self._run(rejected_attempts=1)

        self.assertTrue(result.successful)
        self.assertIs(chain.disposition, CandidateAttemptChainDisposition.ACCEPTED)
        self.assertEqual(len(chain.attempts), 2)
        self.assertEqual(attempt.attempt, 2)
        self.assertIsNotNone(attempt.predecessor_attempt_identity)
        self.assertIsNotNone(attempt.source_predecessor_identity)
        self.assertEqual(head.attempt, 2)
        self.assertIs(head.evidence.outcome, StandardLifecycleCheckpointOutcome.PASSED)

    def test_an_exhausted_chain_ends_on_its_third_failed_attempt(self) -> None:
        result, chain, attempt, head = self._run(rejected_attempts=3)

        self.assertFalse(result.successful)
        self.assertIs(chain.disposition, CandidateAttemptChainDisposition.EXHAUSTED)
        self.assertEqual(len(chain.attempts), 3)
        self.assertEqual(attempt.attempt, 3)
        self.assertEqual(head.attempt, 3)
        self.assertIs(head.evidence.outcome, StandardLifecycleCheckpointOutcome.FAILED)
        self.assertEqual(
            head.evidence.failure_code, "builder.generated-source-rejected"
        )

    def test_rebuild_reports_the_replacements_it_ran(self) -> None:
        result, _chain, _attempt, _head = self._run(rejected_attempts=1)

        self.assertEqual(_repair_report(result), {"attempts": 1, "status": "repaired"})
        self.assertEqual(
            _repair_report(mock.Mock(candidate_attempt_chains=())),
            {"attempts": 0, "status": "not-needed"},
        )


if __name__ == "__main__":
    unittest.main()
