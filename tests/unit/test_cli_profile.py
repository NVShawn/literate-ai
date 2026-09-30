from __future__ import annotations

import unittest

from literate_ai.cli.profile import build_profile_report


class CliProfileTests(unittest.TestCase):
    def test_report_aggregates_operations_subprocesses_and_hotspots(self) -> None:
        report = build_profile_report(
            [
                {
                    "ts": "2026-08-15T00:00:00+00:00",
                    "event": "operation.end",
                    "operation": "build",
                    "duration_ms": 10,
                },
                {
                    "ts": "2026-08-15T00:00:00.025000+00:00",
                    "event": "operation.error",
                    "operation": "build",
                    "duration_ms": 20,
                    "error": "failed",
                },
                {
                    "ts": "2026-08-15T00:00:00.030000+00:00",
                    "event": "subprocess.exit",
                    "argv": ["python", "-m", "unittest"],
                    "duration_ms": 30,
                    "exit_code": 0,
                },
            ],
            {"passed": True},
        )

        self.assertEqual(report["schema"], "literate-ai/profile-report@1")
        self.assertEqual(report["record_count"], 3)
        self.assertEqual(report["total_duration_ms"], 30.0)
        self.assertEqual(report["operations"][0]["error_count"], 1)
        self.assertEqual(report["operations"][0]["mean_ms"], 15.0)
        self.assertEqual(report["subprocesses"][0]["argv0"], "python")
        self.assertEqual(report["hotspots"][0]["duration_ms"], 30)
        self.assertEqual(report["command_result"], {"passed": True})


if __name__ == "__main__":
    unittest.main()
