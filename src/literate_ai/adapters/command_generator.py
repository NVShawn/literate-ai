"""Generation dispatch shares admitted capacity and imports verified source custody."""

from functools import partial
from threading import Lock

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_generate_record import GenerateWorkerInput
from literate_ai.adapters.action_generate_result import GenerateWorkerResult
from literate_ai.adapters.action_generate_source import receive_generated_source
from literate_ai.adapters.retained_source import RetainedSourceInput
from literate_ai.adapters.source_generation import CachedCodingCliSourceGenerationRunner
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
    GeneratedSourceCandidate,
    SourceDerivationCacheKey,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.storage import FileSystemCAS


class CommandSourceGenerator:
    """Reserve a generation worker before consuming a lifecycle executor thread."""

    def __init__(
        self,
        indexer,
        admission,
        *,
        cache_key_provider,
        result_source=None,
        candidate_cas=None,
        retained_source=None,
        retained_source_authorization=None,
    ):
        if (
            indexer.catalog.identity != admission.catalog.identity
            or indexer.workers != admission.workers
        ):
            raise ValueError("GENERATE and INDEX must share the admitted worker pool")
        if result_source is not None and not callable(result_source):
            raise TypeError("GENERATE return transport must be a private callable")
        self.indexer, self.admission = indexer, admission
        if not callable(cache_key_provider):
            raise TypeError("GENERATE requires a private cache-key planner")
        self.cache_key_provider = cache_key_provider
        self._candidate_keys = {}
        self._key_lock = Lock()
        if candidate_cas is not None and not isinstance(candidate_cas, FileSystemCAS):
            raise TypeError("candidate custody requires filesystem CAS")
        self.candidate_cas = candidate_cas if candidate_cas is not None else indexer.cas
        if retained_source is not None:
            if not isinstance(retained_source, RetainedSourceInput):
                raise TypeError("retained source must be typed")
            retained_source.require_authorization(retained_source_authorization)
        elif retained_source_authorization is not None:
            raise ValueError("retained authorization requires source input")
        self.retained_source = retained_source
        self.retained_source_authorization = retained_source_authorization
        self.result_source = result_source
        self.eligible = tuple(
            worker.worker_id
            for worker in indexer.workers
            if admission.supports_phase(worker, LifecycleActionKind.GENERATE)
        )
        if not self.eligible:
            raise ValueError("no admitted GENERATE worker")
        self.nodes = {
            node.component_revision: node
            for node in plan_lifecycle_action_dag(
                indexer.execution_plan, worker_ids=self.eligible
            )
            if node.kind is LifecycleActionKind.GENERATE
        }

    def planned_cache_key(self, prepared):
        key = self.cache_key_provider(prepared)
        if not isinstance(
            key, SourceDerivationCacheKey
        ) or key.recipe_identity != ContentIdentity.parse_uri(prepared.recipe.identity):
            raise ActionWireError(
                "action_generate.cache_key_invalid",
                "GENERATION key differs from recipe",
            )
        return key

    def cache_key_for_candidate(self, candidate):
        if not isinstance(candidate, GeneratedSourceCandidate):
            raise TypeError("candidate must be typed")
        with self._key_lock:
            try:
                return self._candidate_keys[candidate.identity]
            except KeyError as exc:
                raise ActionWireError(
                    "action_generate.cache_key_missing",
                    "candidate has no captured cache key",
                ) from exc

    def record_restored_cache_key(self, candidate, key):
        if not isinstance(candidate, GeneratedSourceCandidate) or not isinstance(
            key, SourceDerivationCacheKey
        ):
            raise TypeError("restored candidate and cache key must be typed")
        if candidate.recipe_identity != key.recipe_identity:
            raise ActionWireError(
                "action_generate.cache_key_invalid", "restored key differs from recipe"
            )
        with self._key_lock:
            existing = self._candidate_keys.get(candidate.identity)
            if existing is not None and existing != key:
                raise ActionWireError(
                    "action_generate.cache_key_changed", "candidate cache key changed"
                )
            self._candidate_keys[candidate.identity] = key

    def _input(self, prepared):
        return GenerateWorkerInput.capture(
            self.indexer.execution_plan,
            prepared,
            self.indexer.cas,
            self.indexer.deadline,
            retained=self.retained_source,
            authorization=self.retained_source_authorization,
        )

    def _eligible_for(self, prepared):
        CachedCodingCliSourceGenerationRunner._require_fresh_workspace(prepared)
        self._input(prepared)
        eligible = tuple(
            worker.worker_id
            for worker in self.indexer.workers
            if worker.worker_id in self.eligible
            and self.admission.supports_phase(worker, LifecycleActionKind.GENERATE)
        )
        if not eligible:
            raise ActionWireError(
                "action_generate.unavailable", "no compatible GENERATE worker"
            )
        return eligible

    def try_reserve_generate(self, prepared):
        return self.indexer.slots.try_reserve(
            partial(self._execute, prepared),
            eligible_worker_ids=self._eligible_for(prepared),
        )

    def __call__(self, prepared):
        with self.indexer.slots.acquire(
            eligible_worker_ids=self._eligible_for(prepared)
        ) as (worker, slot):
            return self._execute(prepared, worker, slot)

    def _execute(self, prepared, worker, slot):
        CachedCodingCliSourceGenerationRunner._require_fresh_workspace(prepared)
        value = self._input(prepared)
        content = value.to_bytes()
        input_identity = record_identity(content)
        cache_key = self.planned_cache_key(prepared)

        def current():
            self.indexer.deadline.remaining()
            self.indexer.require_worker_current(worker)
            if self.planned_cache_key(prepared) != cache_key:
                raise ActionWireError(
                    "action_generate.cache_key_changed",
                    "GENERATION cache authority changed",
                )
            if self._input(prepared) != value or not self.admission.supports_phase(
                worker, LifecycleActionKind.GENERATE
            ):
                raise ActionWireError(
                    "action_generate.authority_changed", "GENERATION authority changed"
                )

        current()
        payload = canonical_json_bytes(
            lifecycle_action_payload(
                value.execution_plan.identity,
                prepared.plan.component_revision,
                LifecycleActionKind.GENERATE,
                prepared.plan.identity,
            )
        )
        records = {input_identity: content, record_identity(payload): payload}
        for record in records.values():
            self.indexer.remember_action_result(record)
        request = LifecycleActionDispatchRequest(
            canonical_identity(
                {
                    "schema": "literate-ai/command-generate-schedule@1",
                    "execution_plan": value.execution_plan.identity.uri,
                    "admission": self.admission.identity.uri,
                }
            ),
            self.nodes[prepared.plan.component_revision],
            worker,
            slot,
            (),
            self.indexer.deadline.identity,
            (input_identity,),
        )
        current()
        results = {}
        outcome = self.indexer._dispatcher(records, results).dispatch(request)
        if outcome.failure_code is not None:
            raise ActionWireError(outcome.failure_code, "worker GENERATE failed")
        result = results.get(outcome.result_identity)
        if result is None:
            raise ActionWireError(
                "action_generate.result_missing", "worker GENERATE result missing"
            )
        self.indexer.remember_action_result(result)
        selected = self.indexer.catalog.worker(worker.worker_id)
        admitted_result = GenerateWorkerResult.admit(
            result,
            outcome.result_identity,
            input_record=content,
            input_identity=input_identity,
            deadline=self.indexer.deadline,
        )
        if (
            admitted_result.output.candidate.planned_coding_cli_request_identity
            != cache_key.request_identity
        ):
            raise ActionWireError(
                "action_generate.cache_key_invalid",
                "worker generated another planned request",
            )
        output = receive_generated_source(
            content=result,
            result_identity=outcome.result_identity,
            input_record=content,
            input_identity=input_identity,
            prepared=prepared,
            deadline=self.indexer.deadline,
            cas=self.candidate_cas,
            registry=self.indexer.source_trees,
            admission_guard=current,
            blob_source=None
            if self.result_source is None
            else partial(self.result_source, selected),
        )
        self.record_restored_cache_key(output.candidate, cache_key)
        return output
