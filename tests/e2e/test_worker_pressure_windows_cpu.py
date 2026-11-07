"""Native Windows counters preserve bounded, fail-closed pressure evidence."""

import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai import worker_pressure_probe as probe
from literate_ai.adapters import worker_pressure as adapter
from literate_ai.application.worker_capacity import CapacityHealth
from literate_ai.application.worker_pressure import (
    PressurePolicy,
    assess_worker_pressure,
)
from literate_ai.contracts import (
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerKind,
)
from literate_ai.contracts.worker_capacity import CapacityProbeStatus


class WindowsCpuTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "requires the actual Windows API")
    def test_native_windows_local_and_receiver_cpu_satisfy_required_policy(self):
        api = probe.ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x800)
        api.GetActiveProcessorGroupCount.argtypes = []
        api.GetActiveProcessorGroupCount.restype = probe.ctypes.c_uint16
        groups = api.GetActiveProcessorGroupCount()
        self.assertGreater(groups, 0)
        if groups > 1:
            self.skipTest("native positive qualification requires one processor group")
        policy = PressurePolicy(2, 10000, 0, 0, 0, 0, True, False, False)
        local = SimpleNamespace(
            worker=ExecutionWorker("native-local", ExecutionWorkerKind.LOCAL)
        )
        remote = SimpleNamespace(
            worker=ExecutionWorker(
                "native-receiver",
                ExecutionWorkerKind.SSH,
                requirements=ExecutionRequirements(os_family="windows"),
                endpoint="user@worker.invalid",
                workspace="/worker",
            ),
            os_family="windows",
            python_executable=None,
            health_command=None,
        )

        def receive(_command, *, environment, timeout_seconds, input_bytes):
            # Replace only SSH transport: execute the actual production receiver
            # and its native APIs in a separate Windows Python process.
            return subprocess.run(
                adapter._probe_command(sys.executable),
                env=environment,
                input=input_bytes,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )

        for kind, samples in (
            ("local", adapter.observe_worker_pressure(local, policy)),
            (
                "receiver",
                adapter.observe_worker_pressure(remote, policy, runner=receive),
            ),
        ):
            with self.subTest(kind=kind):
                self.assertEqual(len(samples), 2)
                for sample in samples:
                    metric = sample.cpu_utilization_basis_points
                    self.assertEqual(metric.status, CapacityProbeStatus.MEASURED)
                    self.assertIsInstance(metric.value, int)
                    self.assertGreaterEqual(metric.value, 0)
                    self.assertLessEqual(metric.value, 10000)
                finding = next(
                    item
                    for item in assess_worker_pressure(policy, samples)
                    if item.resource == "cpu"
                )
                self.assertTrue(finding.required)
                self.assertEqual(finding.health, CapacityHealth.HEALTHY)
        self.assertEqual(api.GetActiveProcessorGroupCount(), 1)

    def api(self, values, *, groups=1):
        library = Mock()
        library.GetActiveProcessorGroupCount.return_value = groups
        counters = iter(values)

        def read(*pointers):
            for pointer, value in zip(pointers, next(counters), strict=True):
                pointer._obj.low = value & 0xFFFFFFFF
                pointer._obj.high = value >> 32
            return 1

        library.GetSystemTimes.side_effect = read
        return library

    def measure(self, library):
        with (
            patch.object(
                probe.ctypes, "WinDLL", return_value=library, create=True
            ) as load,
            patch.object(probe.time, "sleep") as sleep,
        ):
            result = probe._windows_cpu()
        load.assert_called_once_with("kernel32.dll", use_last_error=True, winmode=0x800)
        return result, sleep

    def test_idle_is_subtracted_once_from_kernel_plus_user_time(self):
        library = self.api([(0, 0, 0), (60, 80, 20)])
        result, sleep = self.measure(library)
        self.assertEqual(result, {"status": "measured", "value": 4000})
        sleep.assert_called_once_with(0.1)
        self.assertEqual(library.GetSystemTimes.call_count, 2)

    def test_high_filetime_words_survive_low_word_wrap(self):
        start = 1 << 32
        result, _ = self.measure(
            self.api(
                [
                    (start - 10, start + 100, start + 40),
                    (start + 10, start + 150, start + 90),
                ]
            )
        )
        self.assertEqual(result, {"status": "measured", "value": 8000})

    def test_zero_reset_and_inconsistent_counters_stay_unavailable(self):
        for values in [
            [(0, 0, 0), (0, 0, 0)],
            [(10, 20, 10), (9, 30, 20)],
            [(0, 20, 10), (1, 19, 20)],
            [(0, 0, 0), (50, 40, 100)],
        ]:
            with self.subTest(values=values):
                result, _ = self.measure(self.api(values))
                self.assertEqual(result, {"status": "unavailable", "value": None})

    def test_uncovered_processor_groups_do_not_report_partial_system_usage(self):
        for groups, status in [(0, "unavailable"), (2, "unsupported")]:
            with self.subTest(groups=groups):
                library = self.api([], groups=groups)
                result, sleep = self.measure(library)
                self.assertEqual(result, {"status": status, "value": None})
                library.GetSystemTimes.assert_not_called()
                sleep.assert_not_called()

    def test_processor_group_change_during_window_is_not_complete_evidence(self):
        library = self.api([(0, 0, 0), (0, 80, 20)])
        library.GetActiveProcessorGroupCount.side_effect = [1, 2]
        result, _ = self.measure(library)
        self.assertEqual(result, {"status": "unsupported", "value": None})

    def test_failed_or_missing_native_api_never_becomes_zero_load(self):
        library = self.api([])
        library.GetSystemTimes.side_effect = None
        library.GetSystemTimes.return_value = 0
        result, _ = self.measure(library)
        self.assertEqual(result, {"status": "unavailable", "value": None})
        with patch.object(
            probe.ctypes, "WinDLL", side_effect=AttributeError, create=True
        ):
            self.assertEqual(
                probe._windows_cpu(), {"status": "unsupported", "value": None}
            )
        with patch.object(probe.ctypes, "WinDLL", side_effect=OSError, create=True):
            self.assertEqual(
                probe._windows_cpu(), {"status": "unavailable", "value": None}
            )

    def test_local_adapter_uses_the_same_windows_provider_and_statuses(self):
        for status, value in [
            ("measured", 2345),
            ("unsupported", None),
            ("unavailable", None),
        ]:
            with (
                self.subTest(status=status),
                patch.object(probe.sys, "platform", "win32"),
                patch.object(
                    probe,
                    "_windows_cpu",
                    return_value={"status": status, "value": value},
                ) as native,
            ):
                result = adapter._cpu_basis_points()
                self.assertEqual(result.status, CapacityProbeStatus(status))
                self.assertEqual(result.value, value)
                native.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
