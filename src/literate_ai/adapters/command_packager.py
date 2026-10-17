"""PACKAGE dispatch shares worker capacity and imports independently verified output."""

from functools import partial

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_package import admit_package_action
from literate_ai.adapters.action_package_execution import _bounded_refs
from literate_ai.adapters.action_package_result import import_package_result
from literate_ai.application.action_dag_planning import (
    lifecycle_action_payload,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes


class CommandProjectPackager:
    def __init__(self, linker, *, verify_package, result_source):
        if not callable(verify_package) or not callable(result_source):
            raise TypeError(
                "PACKAGE requires private verification and return transport"
            )
        self.linker = linker
        self.indexer, self.admission = linker.indexer, linker.admission
        self.verify_package, self.result_source = verify_package, result_source

    def package_local_inputs(self, graph, plan, *, read_blob):
        """Publish declared local inputs and recheck their custody after dispatch."""

        references = _bounded_refs(tuple(item.blob for item in plan.inputs))

        def check_inputs(*, publish):
            for reference in references:
                self.indexer.deadline.remaining()
                content = read_blob(reference)
                if (
                    not isinstance(content, bytes)
                    or len(content) != reference.size
                    or record_identity(content).uri != reference.identity
                ):
                    raise ActionWireError(
                        "action_package.input_changed", "local package input changed"
                    )
                self.indexer.deadline.remaining()
                if (
                    publish
                    and self.indexer.cas.put_bytes(
                        content, media_type=reference.media_type
                    )
                    != reference
                ):
                    raise ActionWireError(
                        "action_package.input_changed", "package input CAS differs"
                    )

        self._eligible(plan)
        self.linker.package_handoff(graph, plan)
        check_inputs(publish=True)
        result = self.package(graph, plan)
        check_inputs(publish=False)
        return result

    def _eligible(self, plan):
        workers = tuple(
            worker.worker_id
            for worker in self.indexer.workers
            if self.admission.supports_package(worker, plan.packager_identity)
        )
        if not workers:
            raise ActionWireError(
                "action_package.unavailable", "no admitted worker for exact packager"
            )
        return workers

    def try_reserve_package(self, graph, plan):
        self.linker.package_handoff(graph, plan)
        return self.indexer.slots.try_reserve(
            partial(self._execute, graph, plan),
            eligible_worker_ids=self._eligible(plan),
        )

    def package(self, graph, plan):
        self.linker.package_handoff(graph, plan)
        with self.indexer.slots.acquire(eligible_worker_ids=self._eligible(plan)) as (
            worker,
            slot,
        ):
            return self._execute(graph, plan, worker, slot)

    def _execute(self, graph, plan, worker, slot):
        value, proof = self.linker.package_handoff(graph, plan)
        proof = dict(proof)

        def current():
            self.indexer.deadline.remaining()
            self.indexer.require_worker_current(worker)
            if not self.admission.supports_package(worker, plan.packager_identity):
                raise ActionWireError(
                    "action_package.authority_changed", "PACKAGE worker profile changed"
                )

        def current_handoff():
            current()
            if self.linker.package_handoff(graph, plan) != (value, proof):
                raise ActionWireError(
                    "action_package.authority_changed", "PACKAGE LINK handoff changed"
                )

        current_handoff()
        raw = value.to_bytes()
        input_id = record_identity(raw)
        node = next(
            node
            for node in plan_lifecycle_action_dag(
                self.indexer.execution_plan, worker_ids=self._eligible(plan)
            )
            if node.kind is LifecycleActionKind.PACKAGE
        )
        payload = canonical_json_bytes(
            lifecycle_action_payload(
                self.indexer.execution_plan.identity,
                self.indexer.execution_plan.root_revision,
                LifecycleActionKind.PACKAGE,
            )
        )
        predecessors = tuple(identity for _, identity in value.link_results)
        request = LifecycleActionDispatchRequest(
            canonical_identity(
                {
                    "schema": "literate-ai/command-package-schedule@1",
                    "execution_plan": self.indexer.execution_plan.identity.uri,
                    "admission": self.admission.identity.uri,
                    "package_plan": plan.identity.uri,
                }
            ),
            node,
            worker,
            slot,
            predecessors,
            self.indexer.deadline.identity,
            (
                input_id,
                *sorted(set(proof) - set(predecessors), key=lambda item: item.uri),
            ),
        )
        records = proof | {input_id: raw, record_identity(payload): payload}
        admit_package_action(
            request,
            self.indexer.deadline,
            records,
            expected_worker_identity=worker.worker_identity,
        )
        returned = {}
        outcome = self.indexer._dispatcher(records, returned).dispatch(request)
        if outcome.failure_code is not None:
            raise ActionWireError(outcome.failure_code, "worker PACKAGE failed")
        content = returned.get(outcome.result_identity)
        if content is None:
            raise ActionWireError(
                "action_package.result_missing", "worker PACKAGE result missing"
            )
        current_handoff()
        result = import_package_result(
            content=content,
            result_identity=outcome.result_identity,
            input_record=raw,
            input_identity=input_id,
            records=proof,
            cas=self.indexer.cas,
            deadline=self.indexer.deadline,
            admission_guard=current,
            verify_package=self.verify_package,
            blob_source=partial(
                self.result_source, self.indexer.catalog.worker(worker.worker_id)
            ),
        )
        current_handoff()
        for record in (*records.values(), content):
            self.indexer.remember_action_result(record)
        return result
