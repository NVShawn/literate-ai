"""Deterministic projection from sample selection to host Flavor mix-ins."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.conformance.support.sample_runner import required_host_flavor_names

REPOSITORY = Path(__file__).resolve().parents[2]
SAMPLES = REPOSITORY / "samples"


class SampleHostToolchainPlanTests(unittest.TestCase):
    def test_regenerative_matrix_projects_language_and_build_flavors(self) -> None:
        self.assertEqual(
            required_host_flavor_names(
                SAMPLES,
                sample_patterns=("regenerative-roundtrip",),
                platform="linux",
            ),
            (
                "build-bazel",
                "lang-cpp",
                "lang-javascript",
                "lang-python",
                "lang-rust",
            ),
        )

    def test_worker_os_is_automatic_not_a_planned_language_mixin(self) -> None:
        self.assertEqual(
            required_host_flavor_names(
                SAMPLES,
                sample_patterns=("durable-split-service",),
                flavor_selectors=("+flavor://literate-ai/os-windows",),
                platform="windows",
            ),
            ("build-make", "lang-javascript"),
        )


if __name__ == "__main__":
    unittest.main()
