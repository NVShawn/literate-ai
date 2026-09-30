"""Portable worker pressure fixtures distinguish utilization from overload."""

import unittest

from literate_ai.application.worker_capacity import CapacityHealth
from literate_ai.application.worker_pressure import (
    GpuPressureSample,
    PressurePolicy,
    PressureSample,
    assess_worker_pressure,
)
from literate_ai.contracts.worker_capacity import CapacityMetric, CapacityProbeStatus


def measured(value):
    return CapacityMetric(CapacityProbeStatus.MEASURED, value)


class WorkerPressureTests(unittest.TestCase):
    def setUp(self):
        self.policy = PressurePolicy(3, 8000, 1024, 100, 512, require_gpu=False)

    def sample(
        self,
        index,
        *,
        cpu=9000,
        memory=4096,
        paging=0,
        progress=None,
        queue=1,
        gpu=(),
    ):
        return PressureSample(
            index,
            measured(cpu),
            measured(memory),
            measured(paging),
            measured(index if progress is None else progress),
            measured(queue),
            gpu,
        )

    def finding(self, samples, resource):
        return next(
            item
            for item in assess_worker_pressure(self.policy, tuple(samples))
            if item.resource == resource
        )

    def test_high_cpu_and_gpu_utilization_with_progress_is_healthy(self):
        gpu = (GpuPressureSample("gpu-0", measured(9900), measured(2048)),)
        samples = [self.sample(index, gpu=gpu) for index in range(3)]

        self.assertEqual(self.finding(samples, "cpu").reason, "busy-progressing")
        self.assertEqual(self.finding(samples, "gpu").health, CapacityHealth.HEALTHY)

    def test_sustained_cpu_with_stalled_progress_is_warning(self):
        samples = [self.sample(index, progress=7) for index in range(3)]

        finding = self.finding(samples, "cpu")
        self.assertEqual(finding.health, CapacityHealth.WARNING)
        self.assertEqual(finding.reason, "sustained-load-impact")

    def test_low_memory_with_paging_is_critical(self):
        samples = [self.sample(index, memory=512, paging=101) for index in range(3)]

        finding = self.finding(samples, "memory")
        self.assertEqual(finding.health, CapacityHealth.CRITICAL)
        self.assertEqual(finding.reason, "sustained-memory-pressure")

    def test_gpu_oom_is_critical_even_after_utilization_falls(self):
        samples = [
            self.sample(
                index,
                gpu=(
                    GpuPressureSample("gpu-0", measured(0), measured(4096), index == 1),
                ),
            )
            for index in range(3)
        ]

        self.assertEqual(self.finding(samples, "gpu").reason, "gpu-allocation-failed")

    def test_denied_and_stale_metrics_remain_distinct_unknowns(self):
        denied = CapacityMetric(CapacityProbeStatus.DENIED)
        samples = [self.sample(0), self.sample(1), self.sample(2)]
        samples[-1] = PressureSample(
            2,
            denied,
            measured(4096),
            measured(0),
            measured(2),
            measured(0),
        )

        finding = self.finding(samples, "cpu")
        self.assertEqual(finding.health, CapacityHealth.UNKNOWN)
        self.assertEqual(finding.reason, "probe-denied")

        stale = CapacityMetric(CapacityProbeStatus.UNAVAILABLE)
        samples[-1] = PressureSample(
            2,
            measured(100),
            stale,
            measured(0),
            measured(2),
            measured(0),
        )
        self.assertEqual(self.finding(samples, "memory").reason, "probe-unavailable")


if __name__ == "__main__":
    unittest.main()
