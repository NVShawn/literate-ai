"""Per-Component LINK dispatch shares capacity and independently reopens proof."""

from functools import partial
from threading import Lock

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_link_dependencies import reopen_link_result_closure
from literate_ai.adapters.action_link_record import LinkWorkerInput
from literate_ai.adapters.action_link_result import LinkWorkerResult
from literate_ai.adapters.action_package_record import PackageWorkerInput
from literate_ai.application.action_dag_planning import (
    lifecycle_action_id,
    lifecycle_action_payload,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes


class CommandComponentLinker:
    def __init__(self, indexer, admission, *, handoff_for, result_source=None):
        if (
            indexer.catalog.identity != admission.catalog.identity
            or indexer.workers != admission.workers
        ):
            raise ValueError("LINK and INDEX must share the admitted worker pool")
        if not callable(handoff_for) or (
            result_source is not None and not callable(result_source)
        ):
            raise TypeError(
                "LINK requires private handoff and explicit return transport"
            )
        self.indexer, self.admission = indexer, admission
        self.handoff_for, self.result_source = handoff_for, result_source
        self.eligible = tuple(
            worker.worker_id
            for worker in indexer.workers
            if admission.supports_phase(worker, LifecycleActionKind.LINK)
        )
        if not self.eligible:
            raise ValueError("no admitted LINK worker")
        self.nodes = {
            node.component_revision: node
            for node in plan_lifecycle_action_dag(
                indexer.execution_plan, worker_ids=self.eligible
            )
            if node.kind is LifecycleActionKind.LINK
        }
        self._completed, self._lock = {}, Lock()

    def package_handoff(self, graph, plan):
        """Capture every completed LINK after rechecking its accepted handoff."""
        self.indexer.deadline.remaining()
        with self._lock:
            completed = dict(self._completed)
        expected = {node.action_id for node in self.nodes.values()}
        if set(completed) != expected:
            raise ActionWireError(
                "action_package.links_incomplete",
                "PACKAGE requires every completed LINK",
            )
        records = {}
        for value, result in completed.values():
            current, _ = self._prepare(
                value.build.plan, value.acceptance_result.evidence
            )
            if current != value:
                raise ActionWireError(
                    "action_package.link_changed", "accepted LINK handoff changed"
                )
            for raw in (value.to_bytes(), result.to_bytes()):
                records[record_identity(raw)] = raw
        manifests = tuple(
            sorted(
                (result.manifest for _, result in completed.values()),
                key=lambda item: item.component_revision.uri,
            )
        )
        if graph.manifests != manifests:
            raise ActionWireError(
                "action_package.graph_mismatch",
                "PACKAGE graph differs from completed LINK",
            )
        package = next(
            node
            for node in plan_lifecycle_action_dag(
                self.indexer.execution_plan, worker_ids=self.eligible
            )
            if node.kind is LifecycleActionKind.PACKAGE
        )
        value = PackageWorkerInput(
            self.indexer.execution_plan,
            graph,
            plan,
            tuple(
                (action, record_identity(completed[action][1].to_bytes()))
                for action in package.predecessor_ids
            ),
        )
        raw = value.to_bytes()
        admitted = PackageWorkerInput.admit(
            raw, record_identity(raw), self.indexer.deadline
        )
        return admitted, records

    def _prepare(self, plan, receipt):
        self.indexer.deadline.remaining()
        accepted_input, accepted_result = self.handoff_for(plan, receipt)
        build = accepted_input.execution_input.build_input
        if (
            build.plan != plan
            or build.execution_plan != self.indexer.execution_plan
            or accepted_result.evidence != receipt
        ):
            raise ActionWireError(
                "action_link.input_changed",
                "LINK handoff differs from current acceptance",
            )
        node = self.nodes[plan.component_revision]
        own_accept = lifecycle_action_id(
            plan.component_revision, LifecycleActionKind.ACCEPT
        )
        with self._lock:
            completed = dict(self._completed)
        records, dependencies = {}, []
        pending = [action for action in node.predecessor_ids if action != own_accept]
        for action in pending:
            if action not in completed:
                raise ActionWireError(
                    "action_link.predecessor_missing",
                    "LINK predecessor is not complete",
                )
            dependencies.append(
                (action, record_identity(completed[action][1].to_bytes()))
            )
        visited = set()
        while pending:
            action = pending.pop()
            if action in visited:
                continue
            visited.add(action)
            value, result = completed[action]
            for raw in (value.to_bytes(), result.to_bytes()):
                records[record_identity(raw)] = raw
            pending.extend(name for name, _ in value.dependency_links)
        value = LinkWorkerInput(accepted_input, accepted_result, tuple(dependencies))
        raw = value.to_bytes()
        value = LinkWorkerInput.admit(raw, record_identity(raw), self.indexer.deadline)
        return value, records

    def _eligible(self):
        eligible = tuple(
            worker.worker_id
            for worker in self.indexer.workers
            if worker.worker_id in self.eligible
            and self.admission.supports_phase(worker, LifecycleActionKind.LINK)
        )
        if not eligible:
            raise ActionWireError(
                "action_link.unavailable", "no compatible LINK worker"
            )
        return eligible

    def try_reserve_link(self, plan, receipt):
        try:
            self._prepare(plan, receipt)
        except ActionWireError as exc:
            if exc.code == "action_link.predecessor_missing":
                return None
            raise
        return self.indexer.slots.try_reserve(
            partial(self._execute, plan, receipt), eligible_worker_ids=self._eligible()
        )

    def link(self, plan, receipt):
        self._prepare(plan, receipt)
        with self.indexer.slots.acquire(eligible_worker_ids=self._eligible()) as (
            worker,
            slot,
        ):
            return self._execute(plan, receipt, worker, slot)

    def _execute(self, plan, receipt, worker, slot):
        value, dependencies = self._prepare(plan, receipt)

        def current():
            self.indexer.deadline.remaining()
            self.indexer.require_worker_current(worker)
            if not self.admission.supports_phase(worker, LifecycleActionKind.LINK):
                raise ActionWireError(
                    "action_link.authority_changed", "LINK authority changed"
                )

        def current_handoff():
            current()
            if self._prepare(plan, receipt) != (value, dependencies):
                raise ActionWireError(
                    "action_link.authority_changed", "LINK handoff changed"
                )

        current_handoff()
        raw = value.to_bytes()
        input_id = record_identity(raw)
        accepted = value.acceptance_result.to_bytes()
        accepted_id = record_identity(accepted)
        payload = canonical_json_bytes(
            lifecycle_action_payload(
                value.build.execution_plan_identity,
                plan.component_revision,
                LifecycleActionKind.LINK,
                value.build.generation_plan_identity,
            )
        )
        records = dependencies | {
            input_id: raw,
            accepted_id: accepted,
            record_identity(payload): payload,
        }
        node = self.nodes[plan.component_revision]
        predecessors = dict(value.dependency_links) | {
            lifecycle_action_id(
                plan.component_revision, LifecycleActionKind.ACCEPT
            ): accepted_id
        }
        predecessor_ids = tuple(predecessors[name] for name in node.predecessor_ids)
        additional = tuple(
            sorted(set(dependencies) - set(predecessor_ids), key=lambda item: item.uri)
        )
        request = LifecycleActionDispatchRequest(
            canonical_identity(
                {
                    "schema": "literate-ai/command-link-schedule@1",
                    "execution_plan": self.indexer.execution_plan.identity.uri,
                    "admission": self.admission.identity.uri,
                }
            ),
            node,
            worker,
            slot,
            predecessor_ids,
            self.indexer.deadline.identity,
            (input_id, *additional),
        )
        returned = {}
        outcome = self.indexer._dispatcher(records, returned).dispatch(request)
        if outcome.failure_code is not None:
            raise ActionWireError(outcome.failure_code, "worker LINK failed")
        content = returned.get(outcome.result_identity)
        if content is None:
            raise ActionWireError(
                "action_link.result_missing", "worker LINK result missing"
            )
        result = LinkWorkerResult.admit(
            content,
            outcome.result_identity,
            input_record=raw,
            input_identity=input_id,
            deadline=self.indexer.deadline,
        )
        current_handoff()
        selected = self.indexer.catalog.worker(worker.worker_id)
        reopen_link_result_closure(
            ((node.action_id, outcome.result_identity),),
            execution_plan_identity=self.indexer.execution_plan.identity,
            records=dependencies | {input_id: raw, outcome.result_identity: content},
            cas=self.indexer.cas,
            deadline=self.indexer.deadline,
            admission_guard=current,
            blob_source=None
            if self.result_source is None
            else partial(self.result_source, selected),
        )
        for record in (*records.values(), content):
            self.indexer.remember_action_result(record)
        current_handoff()
        with self._lock:
            previous = self._completed.get(node.action_id)
            if previous is not None and previous != (value, result):
                raise ActionWireError(
                    "action_link.result_changed", "completed LINK result changed"
                )
            self._completed[node.action_id] = (value, result)
        return result.manifest
