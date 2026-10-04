from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_artifact_exports``."""


from literate_ai.contracts import (
    ExecutionDispatchRequest,
    ExecutionWorker,
    LifecycleDispatchAction,
    canonical_identity,
)


def dispatch_request(worker: ExecutionWorker) -> ExecutionDispatchRequest:
    identities = tuple(canonical_identity({"value": index}) for index in range(7))
    return ExecutionDispatchRequest(
        LifecycleDispatchAction.BUILD,
        "component://example/demo",
        "components/demo",
        worker.target_profile,
        (),
        worker.identity,
        worker.requirements,
        (),
        (),
        *identities,
        None,
        60,
    )
