"""Native Windows counters preserve bounded, fail-closed pressure evidence."""

import unittest
from unittest.mock import Mock, patch

from literate_ai import worker_pressure_probe as probe
from literate_ai.adapters import worker_pressure as adapter
from literate_ai.contracts.worker_capacity import CapacityProbeStatus


class WindowsCpuTests(unittest.TestCase):
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
