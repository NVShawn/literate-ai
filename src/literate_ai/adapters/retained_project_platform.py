"""Match actual local observations to authored requirements and selected Flavors."""

from dataclasses import replace

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
        self.context.require_current()

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
