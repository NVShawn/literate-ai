"""Public-export reference audit contracts."""

from __future__ import annotations

import unittest

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
