"""Production source-index port backed by admitted command-worker slots."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.action_command_dispatch import (
    CommandLifecycleActionDispatcher,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_slots import CommandActionSlots
from literate_ai.adapters.action_source_index import (
    MAX_SOURCE_BYTES,
    MAX_SOURCE_FILES,
    source_generation_result,
)
from literate_ai.adapters.action_transport import supports_action_transport
from literate_ai.adapters.cache.filesystem import _read_regular_file
from literate_ai.adapters.intelligence.standard import GeneratedSourceTreeResolver
from literate_ai.adapters.lifecycle.standard_local import LocalSourceTreeRegistry
from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder
from literate_ai.application.action_dag_planning import (
    lifecycle_action_payload,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
    LifecycleActionWorker,
)
from literate_ai.contracts.executable_components import (
    ComponentExecutionPlan,
    GeneratedSourceCandidate,
)
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
)
from literate_ai.contracts.generation_cache import CachedSourceFile
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.source_index import generated_source_tree_identity
from literate_ai.storage import FileSystemCAS


class CommandGenerationIndexer:
    """Publish current source, execute remotely, and retain exact index evidence."""

    @classmethod
    def from_admission(
        cls,
        execution_plan: ComponentExecutionPlan,
        source_trees: GeneratedSourceTreeResolver,
        source_candidate: Callable[[ContentIdentity], GeneratedSourceCandidate],
        cas: FileSystemCAS,
        admission,
    ) -> CommandGenerationIndexer:
        """Compose live admission into both dispatch and result acceptance."""
        from literate_ai.adapters.action_admission import CommandActionWorkerPool

        if (
            not isinstance(admission, CommandActionWorkerPool)
            or admission.phase is not LifecycleActionKind.INDEX
        ):
            raise TypeError("command indexer requires live INDEX admission")
        return cls(
            execution_plan,
            source_trees,
            source_candidate,
            cas,
            admission.catalog,
            admission.workers,
            admission.deadline,
            cwd=admission.cwd,
            revalidate_worker=admission.revalidate,
            environment=admission.environment,
        )

    def __init__(
        self,
        execution_plan: ComponentExecutionPlan,
        source_trees: GeneratedSourceTreeResolver,
        source_candidate: Callable[[ContentIdentity], GeneratedSourceCandidate],
        cas: FileSystemCAS,
        catalog: ExecutionWorkerCatalog,
        workers: Sequence[LifecycleActionWorker],
        deadline: ActionDispatchDeadline,
        *,
        cwd: Path,
        revalidate_worker: Callable[[LifecycleActionWorker], None],
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(cas, FileSystemCAS) or not callable(source_candidate):
            raise TypeError(
                "command indexing requires source custody and a filesystem CAS"
            )
        self.execution_plan = execution_plan
        self.source_trees = source_trees
        self.source_candidate = source_candidate
        self.cas = cas
        self.catalog = catalog
        self.workers = tuple(sorted(workers, key=lambda worker: worker.worker_id))
        self.deadline = deadline
        self.cwd = cwd
        self.revalidate_worker = revalidate_worker
        self.environment = dict(os.environ if environment is None else environment)
        # Reuse the transport's exact admission checks before creating any work.
        self._dispatcher({}, {})
        if sum(worker.slots for worker in self.workers) > 256:
            raise ActionWireError(
                "action_source.capacity_excessive",
                "index capacity exceeds the global bound",
            )
        for admitted in self.workers:
            selected = catalog.worker(admitted.worker_id)
            if (
                (
                    selected.kind is not ExecutionWorkerKind.COMMAND
                    and not supports_action_transport(selected)
                )
                or selected.identity != admitted.worker_identity
                or admitted.slots > selected.slots
            ):
                raise ActionWireError(
                    "action_source.worker_mismatch",
                    "index admission differs from the action worker",
                )
        self._nodes = {
            node.component_revision: node
            for node in plan_lifecycle_action_dag(
                execution_plan,
                worker_ids=tuple(worker.worker_id for worker in self.workers),
            )
            if node.kind is LifecycleActionKind.INDEX
        }
        self._plans = {
            plan.component_revision: plan for plan in execution_plan.generation_plans
        }
        self.slots = CommandActionSlots(self.workers, self.deadline)
        self._recorder = None

    def _dispatcher(self, records, results):
        return CommandLifecycleActionDispatcher(
            self.catalog,
            self.workers,
            self.deadline,
            cwd=self.cwd,
            input_records=lambda _request: records,
            record_result=lambda identity, content: results.update({identity: content}),
            revalidate_worker=self.revalidate_worker,
            environment=self.environment,
        )

    def retain_evidence_with(self, recorder):
        if not isinstance(recorder, QualificationEvidenceRecorder):
            raise TypeError("index evidence requires a qualification recorder")
        with self.slots.condition:
            if self.slots.started:
                raise ValueError("evidence capture must precede source indexing")
            self._recorder = recorder

    def remember_action_result(self, content):
        if self._recorder is not None:
            self._recorder.remember_bytes(content)

    def try_reserve_index(self, component_revision, source):
        return self.slots.try_reserve(
            lambda worker, slot: self._index_reserved(
                component_revision, source, worker, slot
            )
        )

    def _snapshot(self, source: ContentIdentity) -> dict[str, bytes]:
        # Local registry lookup must not rehash an unbounded tree before this
        # bounded capture. Other resolver implementations own their custody checks.
        resolve = (
            self.source_trees.registered_root
            if isinstance(self.source_trees, LocalSourceTreeRegistry)
            else self.source_trees.resolve
        )
        root = resolve(source)
        require_safe_directory(root)
        pending = [root]
        files = {}
        total = 0
        entries = 0
        while pending:
            directory = pending.pop()
            require_safe_directory(directory)
            with os.scandir(directory) as children:
                for child in children:
                    self.deadline.remaining()
                    entries += 1
                    if entries > 2 * MAX_SOURCE_FILES:
                        raise ActionWireError(
                            "action_source.inventory_excessive",
                            "source inventory exceeds its bound",
                        )
                    if child.name == ".codegraph":
                        continue
                    if child.is_symlink():
                        raise ActionWireError(
                            "action_source.path_unsafe",
                            "source contains a symbolic path",
                        )
                    path = Path(child.path)
                    if child.is_dir(follow_symlinks=False):
                        pending.append(path)
                        continue
                    if len(files) >= MAX_SOURCE_FILES:
                        raise ActionWireError(
                            "action_source.inventory_excessive",
                            "source file count exceeds its bound",
                        )
                    require_safe_directory(path.parent)
                    content = _read_regular_file(
                        path,
                        maximum_bytes=MAX_SOURCE_BYTES - total,
                        code="action_source.snapshot_invalid",
                    )
                    total += len(content)
                    files[path.relative_to(root).as_posix()] = content
        if generated_source_tree_identity(files) != source.uri:
            raise ActionWireError(
                "action_source.changed", "source snapshot differs from its candidate"
            )
        if resolve(source) != root:
            raise ActionWireError(
                "action_source.changed", "source registration changed during capture"
            )
        return files

    def index(
        self, component_revision: ContentIdentity, source: ContentIdentity
    ) -> ContentIdentity:
        candidate = self._candidate(component_revision, source)
        with self.slots.acquire() as (worker, slot):
            return self._index_reserved(
                component_revision, source, worker, slot, candidate
            )

    def _candidate(self, component_revision, source):
        candidate = self.source_candidate(source)
        plan = self._plans.get(component_revision)
        if (
            not isinstance(candidate, GeneratedSourceCandidate)
            or plan is None
            or candidate.component_revision != component_revision
            or candidate.tree_identity != source
            or candidate.component_generation_plan_identity != plan.identity
            or candidate.generation_key_identity != plan.generation_key.identity
        ):
            raise ActionWireError(
                "action_source.candidate_mismatch",
                "source candidate differs from the execution plan",
            )
        return candidate

    def _index_reserved(self, component_revision, source, worker, slot, candidate=None):
        if candidate is None:
            candidate = self._candidate(component_revision, source)
        plan = self._plans[component_revision]
        self.revalidate_worker(worker)
        snapshot = self._snapshot(source)
        files = []
        for path, content in sorted(snapshot.items()):
            self.deadline.remaining()
            files.append(CachedSourceFile(path, self.cas.put_bytes(content)))
        previous = source_generation_result(
            self.execution_plan.identity, candidate, tuple(files)
        )
        payload = canonical_json_bytes(
            lifecycle_action_payload(
                self.execution_plan.identity,
                component_revision,
                LifecycleActionKind.INDEX,
                plan.identity,
            )
        )
        records = {
            record_identity(payload): payload,
            record_identity(previous): previous,
        }
        request = LifecycleActionDispatchRequest(
            canonical_identity(
                {
                    "schema": "literate-ai/command-index-schedule@1",
                    "execution_plan": self.execution_plan.identity.uri,
                    "catalog": self.catalog.identity.uri,
                }
            ),
            self._nodes[component_revision],
            worker,
            slot,
            (record_identity(previous),),
            self.deadline.identity,
        )
        results = {}
        outcome = self._dispatcher(records, results).dispatch(request)
        if outcome.failure_code is not None:
            raise ActionWireError(outcome.failure_code, "worker source indexing failed")
        expected = canonical_json_bytes(
            {
                "schema": "literate-ai/disabled-source-index@1",
                "component_revision": component_revision.uri,
                "source": source.uri,
            }
        )
        if (
            outcome.result_identity != record_identity(expected)
            or results.get(outcome.result_identity) != expected
        ):
            raise ActionWireError(
                "action_source.result_mismatch",
                "worker index differs from the exact source policy",
            )
        self._snapshot(source)
        self.deadline.remaining()
        self.remember_action_result(expected)
        return outcome.result_identity
