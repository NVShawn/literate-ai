"""Public-export reference audit contracts."""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.check_public_export import denied_labels


class PublicExportCheckTests(unittest.TestCase):
    def test_rejects_internal_forge_project_and_provider_references(self) -> None:
        for value in (
            b"git@gitlab-master" + b".nvidia.com:org/private.git",
            b"https://jirasw" + b".nvidia.com/browse/PRIVATE-1",
            b"github.com/NVIDIA" + b"-dev/private-project",
            b"horde-" + b"utilization",
            b"nvidia-inference" + b"/switch" + b"yard/model",
            b"person@" + b"nvidia.com",
            b"jira_issue: OM" + b"PE-107836",
            b"slack_channel: #c" + b"dd-literate-ai",
        ):
            with self.subTest(value=value):
                self.assertTrue(denied_labels(value))

    def test_allows_this_repository_and_public_nvidia_oss(self) -> None:
        for value in (
            b"https://github.com/NVIDIA-dev/literate-ai",
            b"git@github.com:NVIDIA-dev/literate-ai.git",
            b"https://github.com/NVIDIA/SkillEvaluator.git",
            b"https://docs.nvidia.com/cuda/",
            b"https://inference-api.nvidia.com/v1",
        ):
            with self.subTest(value=value):
                self.assertEqual(denied_labels(value), ())

    def test_worker_match_preserves_unanchored_existence_boundaries(self):
        legacy = re.compile(rb"[A-Za-z0-9_.-]+\.hrd\.nvidia\.com", re.I)
        suffix = b".hrd" + b".nvidia.com"
        cases = [b"", b"hrd.nvidia.com", suffix, b"a" + suffix.upper()]
        for prefix in (b"", b"a", b"_", b"-", b".", b"ab.cd", b"A" * 128):
            for leading in (b"", b"!", b"x!", b"\xff"):
                for ending in (b"", b":123", b"/path", b".other"):
                    cases.append(leading + prefix + suffix + ending)
        for content in cases:
            with self.subTest(content=content):
                self.assertEqual(
                    "NVIDIA internal worker host" in denied_labels(content),
                    bool(legacy.search(content)),
                )

    def test_long_suffix_free_input_finishes_within_process_budget(self):
        # Only one Python child evaluates the real matcher; no helpers can escape
        # this finite process deadline. The old greedy search exceeds it.
        script = Path(__file__).resolve().parents[2] / "scripts/check_public_export.py"
        code = (
            "import runpy,sys;"
            "m=runpy.run_path(sys.argv[1]);"
            "print(m['denied_labels'](b'A'*65536))"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code, str(script)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=5,
        )
        self.assertEqual(result.stdout.strip(), b"()")
