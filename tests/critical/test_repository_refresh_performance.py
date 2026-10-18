"""Keep the real refresh custody journey inside its Git launch budget."""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


class RepositoryRefreshPerformanceTests(unittest.TestCase):
    def test_armed_stage_failure_stays_within_500_git_launches(self):
        from tests.support.fixtures_test_repository_refresh_application import (
            RepositoryRefreshApplicationTests,
        )

        original = subprocess.Popen
        launches = 0

        def count_git(argv, *args, **kwargs):
            nonlocal launches
            if isinstance(argv, (tuple, list)) and Path(str(argv[0])).name.lower() in {
                "git",
                "git.exe",
            }:
                launches += 1
            return original(argv, *args, **kwargs)

        result = unittest.TestResult()
        case = RepositoryRefreshApplicationTests(
            "test_armed_stage_preserves_body_error_before_terminal_proof"
        )
        with patch.object(subprocess, "Popen", side_effect=count_git):
            case.run(result)
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.skipped, [])
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertGreater(launches, 0, "real Git journey must run")
        self.assertLessEqual(launches, 500)
