"""Live request ownership for a privately registered retained PACKAGE receiver.

No document decoder creates this capability. The attested receiver supplies its
actual capability measurement and private registration guard; the original SSH
worker and catalog remain bound throughout execution.
"""

import os

from literate_ai.adapters.action_capabilities import ActionWorkerCapabilities
from literate_ai.adapters.action_dispatch_wire import ActionDispatchDeadline
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
)
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerKind,
)
from literate_ai.contracts.identity import ContentIdentity

_LIVE_RECEIVER = object()


class RetainedReceiverContext:
    def __init__(
        self,
        request,
        deadline,
        worker,
        catalog_identity,
        retained_plan_identity,
        capability_identity,
        registration_guard,
        measure_capability,
        cancelled,
        *,
        _seal=None,
    ):
        if _seal is not _LIVE_RECEIVER:
            raise TypeError("retained.receiver.live-measured-context-required")
        self.request, self.deadline, self.worker = request, deadline, worker
        self.catalog_identity = catalog_identity
        self.retained_plan_identity = retained_plan_identity
        self.capability_identity = capability_identity
        self._registration_guard = registration_guard
        self._measure_capability = measure_capability
        self._cancelled = cancelled
        self._pid = os.getpid()
        self._open = True

    def require_current(self):
        if not self._open or os.getpid() != self._pid:
            raise ValueError("retained.receiver.context-not-live")
        self.deadline.remaining()
        if self._cancelled():
            raise ValueError("retained.receiver.cancelled")
        self._registration_guard()
        observed = self._measure_capability()
        if (
            not isinstance(observed, ActionWorkerCapabilities)
            or observed.capability_identity != self.capability_identity
            or observed.worker_identity != self.worker.identity
            or LifecycleActionKind.PACKAGE not in observed.actions
        ):
            raise ValueError("retained.receiver.capability-changed")
        self.deadline.remaining()

    def require_plan(self, plan):
        self.require_current()
        if (
            plan.identity != self.retained_plan_identity
            or plan.profile.worker_identity != self.worker.identity
            or plan.profile.target != self.worker.target_profile
        ):
            raise ValueError("retained.receiver.registered-plan-mismatch")

    def close(self):
        self._open = False


def _admit_retained_receiver_context(
    request,
    deadline,
    *,
    worker,
    catalog_identity,
    retained_plan_identity,
    capability_identity,
    registration_guard,
    measure_capability,
    cancelled=lambda: False,
):
    """Private receiver composition after decoding and authenticating its request.

    Arguments describing execution authority come from private startup settings,
    never paths, callables, or expected measurements supplied on the action wire.
    The measurement callback performs the existing receiver capability probe.
    """
    if (
        not isinstance(request, LifecycleActionDispatchRequest)
        or not isinstance(deadline, ActionDispatchDeadline)
        or not isinstance(worker, ExecutionWorker)
        or worker.kind is not ExecutionWorkerKind.SSH
        or request.action.kind is not LifecycleActionKind.PACKAGE
        or any(
            not isinstance(value, ContentIdentity)
            for value in (catalog_identity, retained_plan_identity, capability_identity)
        )
        or request.worker.worker_identity != worker.identity
        or request.worker.worker_id != worker.worker_id
        or request.worker.catalog_identity != catalog_identity
        or request.deadline_identity != deadline.identity
        or not all(
            callable(value)
            for value in (registration_guard, measure_capability, cancelled)
        )
    ):
        raise TypeError("retained.receiver.exact-private-request-binding-required")
    context = RetainedReceiverContext(
        request,
        deadline,
        worker,
        catalog_identity,
        retained_plan_identity,
        capability_identity,
        registration_guard,
        measure_capability,
        cancelled,
        _seal=_LIVE_RECEIVER,
    )
    context.require_current()
    return context
