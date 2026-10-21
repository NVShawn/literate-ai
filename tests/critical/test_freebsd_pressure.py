"""FreeBSD pressure uses native counters and preserves unknown observations."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from literate_ai import worker_pressure_probe as probe
from literate_ai.adapters import worker_pressure


class FreeBSDPressureTests(unittest.TestCase):
    def test_cpu_uses_tick_delta_instead_of_load_average(self):
        with (
            patch("sys.platform", "freebsd16"),
            patch.object(
                probe,
                "_freebsd_cpu_ticks",
                side_effect=((10, 0, 10, 0, 80), (20, 0, 20, 0, 160)),
            ),
            patch.object(probe.time, "sleep"),
        ):
            self.assertEqual(probe._cpu(), {"status": "measured", "value": 2000})

    def test_memory_counts_free_and_inactive_pages_with_native_page_size(self):
        def run(argv, **kwargs):
            self.assertEqual(
                argv,
                (
                    "/sbin/sysctl",
                    "-n",
                    "hw.pagesize",
                    "vm.stats.vm.v_free_count",
                    "vm.stats.vm.v_inactive_count",
                ),
            )
            self.assertEqual(kwargs["timeout"], 2)
            return subprocess.CompletedProcess(argv, 0, "4096\n100\n200\n", "")

        with (
            patch("sys.platform", "freebsd16"),
            patch.object(probe.subprocess, "run", run),
        ):
            self.assertEqual(probe._memory(), {"status": "measured", "value": 1228800})
            self.assertEqual(worker_pressure._available_memory().value, 1228800)

    def test_paging_counts_native_swap_bytes(self):
        with (
            patch("sys.platform", "freebsd16"),
            patch.object(probe, "_freebsd_sysctls", return_value=(4096, 12, 34)),
            patch.object(probe.time, "monotonic_ns", return_value=123),
        ):
            self.assertEqual(probe._paging(), (188416, 123))

    def test_failed_or_malformed_native_measurement_does_not_claim_health(self):
        with (
            patch("sys.platform", "freebsd16"),
            patch.object(
                probe.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("sysctl", 2),
            ),
        ):
            self.assertEqual(probe._memory(), {"status": "unavailable", "value": None})
            self.assertEqual(probe._cpu(), {"status": "unavailable", "value": None})
            self.assertIsNone(probe._paging())
        with (
            patch("sys.platform", "freebsd16"),
            patch.object(
                probe.subprocess,
                "run",
                return_value=subprocess.CompletedProcess((), 0, "4096\n-1\n2\n", ""),
            ),
        ):
            self.assertEqual(probe._memory(), {"status": "unavailable", "value": None})
