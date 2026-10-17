"""Live command-phase admission and revalidation for production dispatch."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from literate_ai.adapters.action_capabilities import (
    package_profile_identity,
    probe_command_action_capabilities,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.action_transport import supports_action_transport
from literate_ai.application.action_dag_planning import (
    ActionDagPlanningError,
    admit_lifecycle_action_workers,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionKind,
    LifecycleActionWorker,
)
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerCatalog,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.worker_capabilities import WorkerHardwareObservationCatalog


class CommandActionWorkerPool:
    """Bind route, hardware, phase, runtime and health before exposing worker slots.

    Loaders and health admission are custody ports. Phase execution and capability
    probing always use the configured command receiver, never these callbacks.
    """

    def __init__(
        self,
        catalog_loader: Callable[[], ExecutionWorkerCatalog],
        observations_loader: Callable[[], WorkerHardwareObservationCatalog],
        health_admission: Callable[[ExecutionWorker], ContentIdentity],
        deadline: ActionDispatchDeadline,
        *,
        phase: LifecycleActionKind,
        source_handoff: str,
        target_profile: str,
        cwd: Path,
        environment: Mapping[str, str] | None = None,
        maximum_hardware_age: timedelta = timedelta(minutes=5),
    ) -> None:
        if (
            not all(
                callable(item)
                for item in (catalog_loader, observations_loader, health_admission)
            )
            or not isinstance(deadline, ActionDispatchDeadline)
            or not isinstance(phase, LifecycleActionKind)
            or source_handoff not in ("filesystem-cas", "http-cas")
            or not isinstance(target_profile, str)
            or not target_profile
            or not isinstance(maximum_hardware_age, timedelta)
            or not timedelta(0) < maximum_hardware_age <= timedelta(hours=24)
        ):
            raise TypeError("command admission requires bounded typed authority ports")
        self.catalog_loader = catalog_loader
        self.observations_loader = observations_loader
        self.health_admission = health_admission
        self.deadline = deadline
        self.phase = phase
        self.source_handoff = source_handoff
        self.target_profile = target_profile
        self.cwd = cwd
        self.environment = dict(os.environ if environment is None else environment)
        # The receiver's configuration can arrive through this environment, so an
        # attested boundary is valid only while it is the one admission probed.
        self._admitted_environment = dict(self.environment)
        self.maximum_hardware_age = maximum_hardware_age
        deadline.remaining()
        self.catalog = catalog_loader()
        eligible, self.hardware_observations = self._eligible()
        admitted = []
        self._facts = {}
        self._hardware = {}
        eligible_ids = {item.worker_id for item in eligible}
        refusals = [
            (worker.worker_id, "action_admission.not_eligible")
            for worker in self.catalog.workers
            if worker.worker_id not in eligible_ids
        ]
        for item in eligible:
            worker = self.catalog.worker(item.worker_id)
            try:
                health = self._health(worker)
                capability = self._probe(worker)
            except (ActionWireError, OSError):
                # Private transport and policy diagnostics are not catalog output.
                refusals.append((worker.worker_id, "action_admission.unavailable"))
                continue
            self._facts[worker.worker_id] = capability
            self._hardware[worker.worker_id] = self._hardware_facts(
                self.hardware_observations, worker.worker_id
            )
            admitted.append(
                replace(
                    item,
                    observation_identity=canonical_identity(
                        {
                            "schema": "literate-ai/command-action-admission@1",
                            "hardware": item.observation_identity.uri,
                            "capability": capability.identity.uri,
                            "health": health.uri,
                            "phase": phase.value,
                            "source_handoff": source_handoff,
                        }
                    ),
                )
            )
        deadline.remaining()
        if not admitted:
            raise ActionWireError(
                "action_admission.no_workers",
                "no command worker satisfies phase admission",
            )
        self.capabilities = tuple(self._facts[item.worker_id] for item in admitted)
        self.workers = tuple(admitted)
        self.refusals = tuple(refusals)

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/command-action-worker-pool@1",
                "catalog": self.catalog.identity.uri,
                "workers": [worker.identity.uri for worker in self.workers],
                "phase": self.phase.value,
                "source_handoff": self.source_handoff,
                "target_profile": self.target_profile,
                "deadline": self.deadline.identity.uri,
            }
        )

    def _eligible(
        self,
    ) -> tuple[tuple[LifecycleActionWorker, ...], WorkerHardwareObservationCatalog]:
        candidates = tuple(
            worker
            for worker in self.catalog.workers
            if supports_action_transport(worker)
        )
        if not candidates:
            raise ActionWireError(
                "action_admission.no_workers",
                "no command worker declares the action protocol",
            )
        selected = ExecutionWorkerCatalog(candidates)
        observations = self.observations_loader()
        if self.catalog_loader().identity != self.catalog.identity:
            raise ActionWireError(
                "action_admission.route_changed",
                "private catalog changed during hardware collection",
            )
        try:
            workers = admit_lifecycle_action_workers(
                selected,
                observations,
                maximum_age=self.maximum_hardware_age,
                target_profile=self.target_profile,
            )
        except ActionDagPlanningError as exc:
            raise ActionWireError(
                "action_admission.hardware_unavailable",
                "no bounded compatible command-worker hardware admission",
            ) from exc
        return (
            tuple(
                replace(worker, catalog_identity=self.catalog.identity)
                for worker in workers
            ),
            observations,
        )

    @staticmethod
    def _hardware_facts(
        observations: WorkerHardwareObservationCatalog, worker_id: str
    ) -> ContentIdentity:
        facts = observations.worker(worker_id).to_dict()
        # The admission identity binds the original timestamp. Revalidation compares
        # all measured facts after independently requiring current freshness.
        del facts["observed_at"]
        return canonical_identity(facts)

    def _health(self, worker: ExecutionWorker) -> ContentIdentity:
        evidence = self.health_admission(worker)
        if not isinstance(evidence, ContentIdentity):
            raise ActionWireError(
                "action_admission.health_missing",
                "health admission requires typed evidence",
            )
        return evidence

    def _probe(self, worker: ExecutionWorker):
        capability = probe_command_action_capabilities(
            worker, self.deadline, cwd=self.cwd, environment=self.environment
        )
        if (
            self.phase not in capability.actions
            or self.source_handoff not in capability.source_handoff
        ):
            raise ActionWireError(
                "action_admission.unsupported",
                "worker does not support the required phase and handoff",
            )
        return capability

    def supports_phase(
        self, worker: LifecycleActionWorker, phase: LifecycleActionKind
    ) -> bool:
        """Read admitted facts; dispatch still revalidates their complete identity."""
        return worker in self.workers and phase in self._facts[worker.worker_id].actions

    def supports_package(
        self, worker: LifecycleActionWorker, packager_identity: ContentIdentity
    ) -> bool:
        """Match the exact private packager before reserving shared capacity."""
        expected = package_profile_identity(packager_identity)
        return self.supports_phase(worker, LifecycleActionKind.PACKAGE) and (
            self._facts[worker.worker_id].package_profile == expected
        )

    def supports_finalize(
        self, worker: LifecycleActionWorker, profile_identity: ContentIdentity
    ) -> bool:
        """Match exact private FINALIZE configuration before reserving capacity."""
        if not isinstance(profile_identity, ContentIdentity):
            raise TypeError("FINALIZE selection requires an exact profile identity")
        return self.supports_phase(worker, LifecycleActionKind.FINALIZE) and (
            self._facts[worker.worker_id].finalize_profile == profile_identity
        )

    def supports_build(self, worker: LifecycleActionWorker, toolchains) -> bool:
        """Require every exact BUILD tool before reserving shared capacity."""
        return self.supports_phase(worker, LifecycleActionKind.BUILD) and set(
            toolchains
        ).issubset(self._facts[worker.worker_id].build_toolchains)

    def supports_test(self, worker: LifecycleActionWorker, toolchains) -> bool:
        """Require every exact TEST runner before reserving shared capacity."""
        return self.supports_phase(worker, LifecycleActionKind.TEST) and set(
            toolchains
        ).issubset(self._facts[worker.worker_id].test_toolchains)

    def supports_execute(self, worker: LifecycleActionWorker, toolchains) -> bool:
        """Require every exact EXECUTE runtime before reserving shared capacity."""
        return self.supports_phase(worker, LifecycleActionKind.EXECUTE) and set(
            toolchains
        ).issubset(self._facts[worker.worker_id].execute_toolchains)

    def require_current(self, worker: LifecycleActionWorker) -> None:
        """Cheap per-read admission: deadline, route, catalog and hardware.

        Guards that run on every record or blob read use this. The full
        storage-health and capability probe in revalidate() runs at each action
        boundary, before dispatch and before any result is admitted.
        """
        self.deadline.remaining()
        if (
            worker not in self.workers
            or self.catalog_loader().identity != self.catalog.identity
        ):
            raise ActionWireError(
                "action_admission.route_changed",
                "worker admission or private catalog changed",
            )
        current, hardware = self._eligible()
        observed = next(
            (item for item in current if item.worker_id == worker.worker_id), None
        )
        if (
            observed is None
            or self._hardware_facts(hardware, worker.worker_id)
            != self._hardware[worker.worker_id]
        ):
            raise ActionWireError(
                "action_admission.hardware_changed",
                "hardware observation is no longer the admitted observation",
            )
        self.hardware_observations = hardware
        self.deadline.remaining()

    def attested_boundary(self, worker: LifecycleActionWorker) -> ContentIdentity:
        """Action-boundary admission whose capability the receiver measures.

        Runs every revalidate() check except the capability probe and returns the
        admitted capability identity. The caller must send it with the action and
        accept the action only when the receiver attests that it measured exactly
        this capability before and after the action; otherwise it must revalidate.
        """
        if self.environment != self._admitted_environment:
            # Dispatchers may hold an older copy, so probe with the current one.
            self.revalidate(worker)
        else:
            self.require_current(worker)
            self._health(self.catalog.worker(worker.worker_id))
            self.deadline.remaining()
        return self._facts[worker.worker_id].capability_identity

    def revalidate(self, worker: LifecycleActionWorker) -> None:
        self.require_current(worker)
        selected = self.catalog.worker(worker.worker_id)
        self._health(selected)
        if (
            self._probe(selected).capability_identity
            != self._facts[worker.worker_id].capability_identity
        ):
            raise ActionWireError(
                "action_admission.runtime_changed",
                "worker runtime or supported capabilities changed",
            )
        self.deadline.remaining()
