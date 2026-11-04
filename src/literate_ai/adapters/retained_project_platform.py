"""Match actual local observations to authored requirements and selected Flavors."""

import os
from dataclasses import dataclass, replace

from literate_ai.adapters.worker_capabilities import (
    WorkerCapabilityProbeError,
    observe_receiver_host,
    probe_worker_capabilities,
)
from literate_ai.bootstrap.host_install_requirements import normalize_architecture
from literate_ai.contracts.execution_dispatch import (
    ExecutionRequirements,
    ExecutionWorkerKind,
)
from literate_ai.contracts.flavors import FlavorAxis
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.retained_project_worker import RetainedLocalWorkerEvidence
from literate_ai.contracts.worker_capabilities import WorkerHardwareObservation


def require_retained_worker_platform(worker, locks, observation):
    if (
        not isinstance(observation, WorkerHardwareObservation)
        or observation.worker_id != worker.worker_id
    ):
        raise ValueError("retained.worker.observation-mismatch")
    observed = replace(
        observation,
        cpu_architecture=normalize_architecture(observation.cpu_architecture),
    )
    required = worker.requirements
    if required.cpu_architecture is not None:
        required = replace(
            required, cpu_architecture=normalize_architecture(required.cpu_architecture)
        )
    if not observed.satisfies(required):
        raise ValueError("retained.worker.requirements-not-satisfied")
    for lock in locks:
        for node in lock.nodes:
            for slot in node.target_flavor_selection.slots:
                for value in slot.selected:
                    if (
                        slot.slot.axis is FlavorAxis.PLATFORM_OS
                        and value.value != observation.os_family
                    ):
                        raise ValueError(
                            "retained.worker.selected-os-mismatch: "
                            f"expected {value.value}; "
                            f"observed {observation.os_family}"
                        )
                    if (
                        slot.slot.axis is FlavorAxis.PLATFORM_ARCHITECTURE
                        and normalize_architecture(value.value)
                        != observed.cpu_architecture
                    ):
                        raise ValueError(
                            "retained.worker.selected-architecture-mismatch: "
                            f"expected {value.value}; "
                            f"observed {observation.cpu_architecture}"
                        )


def _platform_key(observation):
    return (
        observation.os_family,
        observation.os_version,
        normalize_architecture(observation.cpu_architecture),
    )


def require_retained_worker_evidence(evidence, plan, locks):
    evidence.require_plan(plan)
    for observation in evidence.observations:
        require_retained_worker_platform(evidence.worker, locks, observation)
    if _platform_key(evidence.observations[0]) != _platform_key(
        evidence.observations[1]
    ):
        raise ValueError("retained.worker.platform-changed")


class RetainedLocalPlatform:
    def __init__(self, worker, locks, plan):
        if worker.kind is not ExecutionWorkerKind.LOCAL:
            raise ValueError("retained.worker.local-required")
        self.worker, self.locks, self.plan = worker, locks, plan
        self.initial = self.latest = None

    def require_current(self):
        # Authored OS requirements must not choose the local probe implementation:
        # first observe this machine, then compare the original requirements.
        observed_worker = replace(self.worker, requirements=ExecutionRequirements())
        try:
            observation = probe_worker_capabilities(observed_worker)
        except WorkerCapabilityProbeError as exc:
            raise ValueError(
                "retained.worker.observation-unavailable: " + str(exc)
            ) from exc
        require_retained_worker_platform(self.worker, self.locks, observation)
        if self.initial is not None and _platform_key(observation) != _platform_key(
            self.initial
        ):
            raise ValueError("retained.worker.platform-changed")
        self.initial = observation if self.initial is None else self.initial
        self.latest = observation

    def evidence(self):
        result = RetainedLocalWorkerEvidence(
            self.plan.identity, self.worker, (self.initial, self.latest)
        )
        require_retained_worker_evidence(result, self.plan, self.locks)
        return result


_RECEIVER_REPLANNER = object()


class _RetainedReceiverReplanner:
    """A process-local composition of genuine receiver and fresh source checks."""

    def __init__(self, context, plan, observe_authority, *, _seal=None):
        if _seal is not _RECEIVER_REPLANNER:
            raise TypeError("retained.receiver.private-replanner-required")
        self.context, self.plan = context, plan
        self._observe_authority = observe_authority

    def __reduce__(self):
        raise TypeError("retained.receiver.replanner-cannot-be-transported")

    def __call__(self):
        from literate_ai.cli.retained_unit_execution import (
            _CanonicalRetainedAuthorityPlan,
        )

        observed = self.context.require_plan(self.plan)
        if (
            observed is not None
            and type(self._observe_authority) is _CanonicalRetainedAuthorityPlan
            and self._observe_authority._matches_receiver_selection(self.context)
        ):
            observed = self._observe_authority._from_receiver_preparation(observed)
        else:
            # Arbitrary observers may enforce additional owner/currentness
            # assertions. A canonical measurement never replaces their call.
            observed = self._observe_authority()
        if observed != self.plan:
            raise ValueError("retained.receiver.current-plan-changed")
        self.context.require_custody_current()
        return observed

    def observe_platform(self, platform):
        if (
            type(platform) is not RetainedReceiverPlatform
            or platform.context is not self.context
            or platform.plan != self.plan
        ):
            raise ValueError("retained.receiver.replanner-platform-differs")
        # Read-only OS observation issues no authority. The genuine full receiver
        # check and source replan below must succeed before these facts are used.
        self.context.require_custody_current()
        platform._observe_host()
        return self()


def _compose_receiver_current_plan(context, plan, observe_authority):
    from literate_ai.adapters.retained_receiver_context import RetainedReceiverContext
    from literate_ai.contracts.retained_project_lifecycle import RetainedProjectPlan

    if (
        type(context) is not RetainedReceiverContext
        or not isinstance(plan, RetainedProjectPlan)
        or not callable(observe_authority)
    ):
        raise TypeError("retained.receiver.genuine-replanner-inputs-required")
    context.require_plan(plan)
    return _RetainedReceiverReplanner(
        context, plan, observe_authority, _seal=_RECEIVER_REPLANNER
    )


_PLATFORM_REPLANNER = object()


@dataclass(frozen=True, slots=True, init=False, eq=False)
class _RetainedReceiverPlatformReplanner:
    """Original receiver/platform composition; each call prepares afresh."""

    platform: object
    replanner: object
    _pid: int
    _seal: object

    def __init__(self, platform, replanner, *, _seal=None):
        if _seal is not _PLATFORM_REPLANNER:
            raise TypeError("retained.receiver.private-platform-replanner-required")
        for name, value in {
            "platform": platform,
            "replanner": replanner,
            "_pid": os.getpid(),
            "_seal": _seal,
        }.items():
            object.__setattr__(self, name, value)

    def __reduce__(self):
        raise TypeError("retained.receiver.platform-replanner-cannot-be-transported")

    def covers(self, context, plan):
        return (
            self._seal is _PLATFORM_REPLANNER
            and self._pid == os.getpid()
            and type(self.platform) is RetainedReceiverPlatform
            and type(self.replanner) is _RetainedReceiverReplanner
            and self.platform.context is context
            and self.replanner.context is context
            and context.has_canonical_current_plan()
            and self.platform.plan is plan
            and self.replanner.plan is plan
        )

    def __call__(self):
        if not self.covers(self.platform.context, self.platform.plan):
            raise ValueError("retained.receiver.platform-replanner-not-original")
        return self.platform.current_plan(self.replanner)


def _receiver_platform_current_plan(platform, replanner):
    if (
        type(platform) is not RetainedReceiverPlatform
        or type(replanner) is not _RetainedReceiverReplanner
        or not platform.context.has_canonical_current_plan()
    ):
        return None
    current = _RetainedReceiverPlatformReplanner(
        platform, replanner, _seal=_PLATFORM_REPLANNER
    )
    if not current.covers(platform.context, platform.plan):
        raise ValueError("retained.receiver.replanner-platform-differs")
    return current


def _covers_receiver_current_plan(current, context, plan):
    return type(current) is _RetainedReceiverPlatformReplanner and current.covers(
        context, plan
    )


class RetainedReceiverPlatform:
    """Actual receiver host facts under a live original SSH request context.

    This owner, rather than parsed evidence, is required by retained admission.
    It never routes a probe through SSH or substitutes a LOCAL worker identity.
    """

    def __init__(self, context, locks, plan):
        from literate_ai.adapters.retained_receiver_context import (
            RetainedReceiverContext,
        )

        if not isinstance(context, RetainedReceiverContext):
            raise TypeError("retained.receiver.live-measured-context-required")
        context.require_plan(plan)
        self.context, self.worker, self.locks, self.plan = (
            context,
            context.worker,
            locks,
            plan,
        )
        self.initial = self.latest = None

    def require_current(self):
        self.context.require_plan(self.plan)
        self._observe_host()
        self.context.require_current()

    def current_plan(self, observe_authority):
        if type(observe_authority) is _RetainedReceiverReplanner:
            return observe_authority.observe_platform(self)
        self.require_current()
        return observe_authority()

    def _observe_host(self):
        try:
            observation = observe_receiver_host(
                self.worker.worker_id,
                timeout_seconds=max(1, min(20, int(self.context.deadline.remaining()))),
            )
        except WorkerCapabilityProbeError as exc:
            raise ValueError("retained.receiver.observation-unavailable") from exc
        require_retained_worker_platform(self.worker, self.locks, observation)
        if self.initial is not None and _platform_key(observation) != _platform_key(
            self.initial
        ):
            raise ValueError("retained.worker.platform-changed")
        self.initial = observation if self.initial is None else self.initial
        self.latest = observation

    def require_plan(self, plan):
        if plan != self.plan or self.initial is None or self.latest is None:
            raise ValueError(
                "retained.receiver.complete-platform-observations-required"
            )
        self.context.require_plan(plan)
        for observation in (self.initial, self.latest):
            require_retained_worker_platform(self.worker, self.locks, observation)
        if _platform_key(self.initial) != _platform_key(self.latest):
            raise ValueError("retained.worker.platform-changed")

    def evidence(self):
        self.require_current()
        self.require_plan(self.plan)
        return self

    def to_dict(self):
        self.require_plan(self.plan)
        return {
            "schema": "literate-ai/retained-receiver-worker-evidence@1",
            "plan_identity": self.plan.identity.uri,
            "worker": self.worker.to_dict(),
            "catalog_identity": self.context.catalog_identity.uri,
            "request_identity": self.context.request.identity.uri,
            "capability_identity": self.context.capability_identity.uri,
            "observations": [v.to_dict() for v in (self.initial, self.latest)],
            "grants_admission": False,
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())
