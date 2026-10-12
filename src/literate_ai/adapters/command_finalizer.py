"""FINALIZE controller shares worker slots and independently verifies returned proof."""

import os
import tempfile
import time
from contextlib import nullcontext
from functools import partial
from pathlib import Path

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_finalize import admit_finalize_action
from literate_ai.adapters.action_finalize_grant_request import (
    decode_finalize_grant_request,
)
from literate_ai.adapters.action_finalize_record import FinalizeWorkerInput
from literate_ai.adapters.action_finalize_result import import_finalize_result
from literate_ai.adapters.action_package_execution import PackageWorkerResult
from literate_ai.application.action_dag_planning import (
    lifecycle_action_payload,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
)
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)


class CommandProjectFinalizer:
    def __init__(
        self,
        linker,
        *,
        profile_identity,
        verify_package,
        result_source,
        verify_stages=None,
        stage_verifier_context=None,
        grant_request_path=None,
        grant_wait_seconds=0,
        grant_poll_seconds=2.0,
    ):
        if not isinstance(profile_identity, ContentIdentity) or not all(
            callable(item) for item in (verify_package, result_source)
        ):
            raise TypeError(
                "FINALIZE requires exact profile, private verifiers and transport"
            )
        if (verify_stages is None) == (stage_verifier_context is None) or not callable(
            verify_stages if stage_verifier_context is None else stage_verifier_context
        ):
            raise TypeError("FINALIZE requires exactly one private stage verifier")
        self.stage_verifier_context = stage_verifier_context
        self.linker = linker
        self.indexer, self.admission = linker.indexer, linker.admission
        self.profile_identity = profile_identity
        self.verify_package, self.verify_stages = verify_package, verify_stages
        self.result_source = result_source
        self.grant_request_path = grant_request_path
        self.grant_wait_seconds = grant_wait_seconds
        self.grant_poll_seconds = grant_poll_seconds

    def _handoff(self, lock, project, graph, plan, package):
        packaged, proof = self.linker.package_handoff(graph, plan)
        package_raw = PackageWorkerResult(
            record_identity(packaged.to_bytes()), package
        ).to_bytes()
        package_id = record_identity(package_raw)
        value = FinalizeWorkerInput(lock, project, packaged, package_id)
        raw = value.to_bytes()
        FinalizeWorkerInput.admit(raw, record_identity(raw), self.indexer.deadline)
        if packaged.execution_plan != self.indexer.execution_plan:
            raise ActionWireError(
                "action_finalize.authority_changed", "execution plan differs"
            )
        return value, dict(proof) | {package_id: package_raw}

    def _eligible(self):
        workers = tuple(
            worker.worker_id
            for worker in self.indexer.workers
            if self.admission.supports_finalize(worker, self.profile_identity)
        )
        if not workers:
            raise ActionWireError(
                "action_finalize.unavailable",
                "no admitted worker for exact FINALIZE profile",
            )
        return workers

    def try_reserve_finalize(self, lock, project, graph, plan, package):
        self._handoff(lock, project, graph, plan, package)
        return self.indexer.slots.try_reserve(
            partial(self._execute, lock, project, graph, plan, package),
            eligible_worker_ids=self._eligible(),
        )

    def finalize(self, lock, project, graph, plan, package):
        self._handoff(lock, project, graph, plan, package)
        with self.indexer.slots.acquire(eligible_worker_ids=self._eligible()) as (
            worker,
            slot,
        ):
            return self._execute(lock, project, graph, plan, package, worker, slot)

    def _request(self, lock, project, graph, plan, package, worker, slot):
        """Build the exact FINALIZE dispatch shared by execution and description."""
        value, proof = self._handoff(lock, project, graph, plan, package)
        proof = dict(proof)

        def current():
            self.indexer.deadline.remaining()
            self.indexer.require_worker_current(worker)
            if not self.admission.supports_finalize(worker, self.profile_identity):
                raise ActionWireError(
                    "action_finalize.authority_changed",
                    "FINALIZE worker profile changed",
                )

        def current_handoff():
            current()
            if self._handoff(lock, project, graph, plan, package) != (value, proof):
                raise ActionWireError(
                    "action_finalize.authority_changed", "FINALIZE LINK handoff changed"
                )

        current_handoff()
        raw = value.to_bytes()
        input_id = record_identity(raw)
        node = next(
            node
            for node in plan_lifecycle_action_dag(
                self.indexer.execution_plan, worker_ids=self._eligible()
            )
            if node.kind is LifecycleActionKind.FINALIZE
        )
        payload = canonical_json_bytes(
            lifecycle_action_payload(
                self.indexer.execution_plan.identity,
                self.indexer.execution_plan.root_revision,
                LifecycleActionKind.FINALIZE,
            )
        )
        predecessors = (value.package_result_identity,)
        request = LifecycleActionDispatchRequest(
            canonical_identity(
                {
                    "schema": "literate-ai/command-finalize-schedule@1",
                    "execution_plan": self.indexer.execution_plan.identity.uri,
                    "admission": self.admission.identity.uri,
                    "input": input_id.uri,
                    "profile": self.profile_identity.uri,
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
        admit_finalize_action(
            request,
            self.indexer.deadline,
            records,
            expected_worker_identity=worker.worker_identity,
        )
        return value, proof, raw, input_id, request, records, current, current_handoff

    def describe_grant(self, lock, project, graph, plan, package):
        """Ask the selected receiver for the exact grant request; execute nothing.

        The receiver stages and measures the planned inputs. The returned request
        is accepted only when it reconstructs from this planned intent; whether
        its private runtime is acceptable is the operator's decision.
        """
        self._handoff(lock, project, graph, plan, package)
        with self.indexer.slots.acquire(eligible_worker_ids=self._eligible()) as (
            worker,
            slot,
        ):
            value, _, _, _, request, records, _, current_handoff = self._request(
                lock, project, graph, plan, package, worker, slot
            )
            described = self.indexer._dispatcher(records, {}).describe(
                request, mode="--describe-finalize-grant"
            )
            if not isinstance(described, bytes):
                raise ActionWireError(
                    described.failure_code or "action_finalize.grant_request_invalid",
                    "worker FINALIZE grant description failed",
                )
            planned = decode_finalize_grant_request(described, value)
            current_handoff()
            return planned

    def _write_grant_request(self, value, request, records, current_handoff):
        """Describe the refused request on the held slot and hand it to the operator."""
        described = self.indexer._dispatcher(records, {}).describe(
            request, mode="--describe-finalize-grant"
        )
        if not isinstance(described, bytes):
            raise ActionWireError(
                described.failure_code or "action_finalize.grant_request_invalid",
                "worker FINALIZE grant description failed",
            )
        decode_finalize_grant_request(described, value)
        current_handoff()
        path = self.grant_request_path
        # An exclusive, unguessable name never follows a planted symlink.
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(described)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def _execute(self, lock, project, graph, plan, package, worker, slot):
        value, proof, raw, input_id, request, records, current, current_handoff = (
            self._request(lock, project, graph, plan, package, worker, slot)
        )
        returned = {}
        outcome = self.indexer._dispatcher(records, returned).dispatch(request)
        if (
            outcome.failure_code == "action_finalize.grant_unavailable"
            and self.grant_request_path is not None
        ):
            # The input is exact to this run, so the operator must grant it
            # while the run waits; a later rerun would need a new grant.
            self._write_grant_request(value, request, records, current_handoff)
            waited_until = time.monotonic() + self.grant_wait_seconds
            while outcome.failure_code == "action_finalize.grant_unavailable":
                if (
                    time.monotonic() + self.grant_poll_seconds > waited_until
                    or self.indexer.deadline.remaining() <= self.grant_poll_seconds
                ):
                    raise ActionWireError(
                        outcome.failure_code,
                        "worker FINALIZE has no grant; the exact request to "
                        f"authorize is in {self.grant_request_path}",
                    )
                time.sleep(self.grant_poll_seconds)
                current_handoff()
                returned = {}
                outcome = self.indexer._dispatcher(records, returned).dispatch(request)
        if outcome.failure_code is not None:
            raise ActionWireError(outcome.failure_code, "worker FINALIZE failed")
        content = returned.get(outcome.result_identity)
        if content is None:
            raise ActionWireError(
                "action_finalize.result_missing", "worker FINALIZE result missing"
            )
        current_handoff()
        import_inputs = dict(
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
        verification = (
            nullcontext(self.verify_stages)
            if self.stage_verifier_context is None
            else self.stage_verifier_context(**import_inputs)
        )
        with verification as verify_stages:
            result = import_finalize_result(
                content=content,
                result_identity=outcome.result_identity,
                verify_stages=verify_stages,
                **import_inputs,
            )
        current_handoff()
        for record in (*records.values(), content):
            self.indexer.remember_action_result(record)
        return result
