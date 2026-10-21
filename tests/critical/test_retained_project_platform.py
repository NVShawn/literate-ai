"""Actual-host policy is separate from worker labels and portable command success."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch

from literate_ai.adapters.retained_project_platform import (
    RetainedLocalPlatform,
    require_retained_worker_evidence,
    require_retained_worker_platform,
)
from literate_ai.adapters.worker_capabilities import WorkerCapabilityProbeError
from literate_ai.contracts import (
    ComponentLock,
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerKind,
)
from literate_ai.contracts.component_locking import ComponentLockNode
from literate_ai.contracts.executable_components import (
    NodeFlavorSlotResolution,
    NodeTargetFlavorSelection,
    SelectedNodeFlavor,
)
from literate_ai.contracts.flavors import FlavorAxis, FlavorCardinality, FlavorSlot
from literate_ai.contracts.retained_project_worker import RetainedLocalWorkerEvidence
from literate_ai.contracts.worker_capabilities import (
    NvidiaProbeStatus,
    WorkerHardwareObservation,
)
from tests.support import fixtures_test_component_lock_contracts as locked
from tests.support.fixtures_retained_project_sbom import retained_project_sbom_fixture


def observation(family="linux", architecture="x86_64"):
    return WorkerHardwareObservation(
        "local",
        datetime.now(UTC).isoformat(),
        family,
        family,
        "test-version",
        architecture,
        2,
        4,
        4096,
        (),
        NvidiaProbeStatus.NOT_APPLICABLE
        if family == "macos"
        else NvidiaProbeStatus.ABSENT,
    )


def platform_lock(axis, value):
    slot = FlavorSlot(
        "platform", axis, FlavorCardinality.EXACTLY_ONE, "native-platform"
    )
    authoring = replace(locked.component_authoring("platform"), flavor_slots=(slot,))
    revision = locked.locked_revision(
        "platform", selected_flavor=value, authoring=authoring
    )
    target, policy = (
        locked.identity("platform-target"),
        locked.identity("selection-policy"),
    )
    selection = NodeTargetFlavorSelection(
        revision.identity,
        "host",
        target,
        policy,
        (
            NodeFlavorSlotResolution(
                slot, (SelectedNodeFlavor(value, locked.identity("flavor-" + value)),)
            ),
        ),
    )
    node = ComponentLockNode(revision, selection, ())
    return ComponentLock(
        "host",
        target,
        policy,
        locked.identity("resolver"),
        revision.identity,
        (node,),
        (),
        _authorings=(authoring,),
    )


class RetainedProjectPlatformTests(unittest.TestCase):
    def setUp(self):
        self.worker = ExecutionWorker("local", ExecutionWorkerKind.LOCAL)
        self.plan, self.locks = retained_project_sbom_fixture()
        self.plan = replace(
            self.plan,
            profile=replace(self.plan.profile, worker_identity=self.worker.identity),
        )

    def test_real_selected_os_must_match_observation(self):
        lock = platform_lock(FlavorAxis.PLATFORM_OS, "freebsd")
        require_retained_worker_platform(self.worker, (lock,), observation("freebsd"))
        with self.assertRaisesRegex(ValueError, "selected-os-mismatch"):
            require_retained_worker_platform(self.worker, (lock,), observation("linux"))

    def test_architecture_aliases_match_but_different_architecture_refuses(self):
        lock = platform_lock(FlavorAxis.PLATFORM_ARCHITECTURE, "amd64")
        require_retained_worker_platform(self.worker, (lock,), observation())
        with self.assertRaisesRegex(ValueError, "selected-architecture-mismatch"):
            require_retained_worker_platform(
                self.worker, (lock,), observation(architecture="arm64")
            )

    def test_authored_capacity_and_worker_identity_require_real_satisfaction(self):
        worker = replace(
            self.worker, requirements=ExecutionRequirements(minimum_memory_mib=8192)
        )
        with self.assertRaisesRegex(ValueError, "requirements-not-satisfied"):
            require_retained_worker_platform(worker, self.locks, observation())
        with self.assertRaisesRegex(ValueError, "observation-mismatch"):
            require_retained_worker_platform(
                self.worker, self.locks, replace(observation(), worker_id="other")
            )

    def test_declared_os_never_selects_local_probe_implementation(self):
        worker = replace(
            self.worker, requirements=ExecutionRequirements(os_family="freebsd")
        )
        guard = RetainedLocalPlatform(worker, self.locks, self.plan)
        with patch(
            "literate_ai.adapters.retained_project_platform.probe_worker_capabilities",
            return_value=observation(),
        ) as probe:
            with self.assertRaisesRegex(ValueError, "requirements-not-satisfied"):
                guard.require_current()
        self.assertIsNone(probe.call_args.args[0].requirements.os_family)

    def test_later_platform_change_refuses_even_without_explicit_os_slot(self):
        guard = RetainedLocalPlatform(self.worker, self.locks, self.plan)
        with patch(
            "literate_ai.adapters.retained_project_platform.probe_worker_capabilities",
            side_effect=[observation(), observation("macos")],
        ):
            guard.require_current()
            with self.assertRaisesRegex(ValueError, "platform-changed"):
                guard.require_current()

    def test_probe_failure_does_not_become_platform_eligibility(self):
        guard = RetainedLocalPlatform(self.worker, self.locks, self.plan)
        with patch(
            "literate_ai.adapters.retained_project_platform.probe_worker_capabilities",
            side_effect=WorkerCapabilityProbeError("worker.probe", "unavailable"),
        ):
            with self.assertRaisesRegex(ValueError, "observation-unavailable"):
                guard.require_current()
        self.assertIsNone(guard.initial)

    def test_bound_evidence_is_data_only_and_cannot_substitute_another_plan(self):
        original = observation()
        evidence = RetainedLocalWorkerEvidence(
            self.plan.identity, self.worker, (original, original)
        )
        evidence.require_plan(self.plan)
        self.assertEqual(
            RetainedLocalWorkerEvidence.from_dict(evidence.to_dict()), evidence
        )
        self.assertFalse(evidence.to_dict()["grants_admission"])
        with self.assertRaises(ValueError):
            RetainedLocalWorkerEvidence.from_dict(
                evidence.to_dict() | {"grants_admission": True}
            )
        with self.assertRaisesRegex(ValueError, "plan-mismatch"):
            evidence.require_plan(replace(self.plan, project_id="changed"))

    def test_inconsistent_observations_cannot_be_admitted_as_a_completed_context(self):
        evidence = RetainedLocalWorkerEvidence(
            self.plan.identity, self.worker, (observation(), observation("macos"))
        )
        with self.assertRaisesRegex(ValueError, "platform-changed"):
            require_retained_worker_evidence(evidence, self.plan, self.locks)

    def test_local_guard_cannot_dispatch_a_remote_probe(self):
        worker = ExecutionWorker(
            "remote", ExecutionWorkerKind.COMMAND, command=("unused-worker",)
        )
        with patch(
            "literate_ai.adapters.retained_project_platform.probe_worker_capabilities"
        ) as probe:
            with self.assertRaisesRegex(ValueError, "local-required"):
                RetainedLocalPlatform(worker, self.locks, self.plan)
        probe.assert_not_called()
