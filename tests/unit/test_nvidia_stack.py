from __future__ import annotations

import unittest

from literate_ai.application.nvidia_stack import NvidiaStackError, resolve_nvidia_stack
from literate_ai.contracts import (
    NvidiaProbeStatus,
    ObservedGpuDevice,
    WorkerHardwareObservation,
)


class NvidiaStackTests(unittest.TestCase):
    def observation(self, capability: str = "8.0") -> WorkerHardwareObservation:
        return WorkerHardwareObservation(
            "gpu-worker",
            "2026-09-01T12:00:00Z",
            "linux",
            "Ubuntu",
            "24.04",
            "x86_64",
            8,
            16,
            32768,
            (
                ObservedGpuDevice(
                    "nvidia",
                    "test-device",
                    0,
                    "GPU-fixture",
                    16384,
                    capability,
                    "575.10",
                ),
            ),
            NvidiaProbeStatus.OK,
        )

    def compatibility(self) -> dict[str, object]:
        def candidate(identifier: str, toolkit: str) -> dict[str, object]:
            return {
                "id": identifier,
                "os_families": ["linux", "windows"],
                "cpu_architectures": ["x86_64"],
                "python_abis": ["cp311"],
                "toolkit_version": toolkit,
                "minimum_driver_versions": {"linux": "570.00", "windows": "570.00"},
                "compute_capabilities": ["8.0", "8.9"],
                "compiler_targets": ["sm_80", "sm_89"],
                "packages": [{"name": "torch", "version": "2.9.0", "sha256": "a" * 64}],
            }

        return {
            "schema": "literate-ai/nvidia-stack-compatibility@1",
            "retrieved_at": "2026-09-01T12:00:00Z",
            "source_urls": ["https://example.invalid/retained-primary-source"],
            "candidates": [candidate("cuda-13", "13.0"), candidate("cuda-12", "12.9")],
        }

    def test_selects_oldest_compatible_exact_candidate_and_binds_inputs(self) -> None:
        selected = resolve_nvidia_stack(
            self.observation(),
            self.compatibility(),
            python_abi="cp311",
            package_pins=("torch==2.9.0",),
        )
        self.assertEqual(selected.candidate["id"], "cuda-12")
        self.assertTrue(selected.observation_identity.startswith("sha256:"))
        self.assertTrue(selected.compatibility_identity.startswith("sha256:"))

    def test_incompatible_capability_and_explicit_pin_fail_closed(self) -> None:
        with self.assertRaises(NvidiaStackError) as capability:
            resolve_nvidia_stack(
                self.observation("9.0"), self.compatibility(), python_abi="cp311"
            )
        self.assertEqual(
            capability.exception.code, "nvidia_stack.no_compatible_candidate"
        )
        with self.assertRaises(NvidiaStackError):
            resolve_nvidia_stack(
                self.observation(),
                self.compatibility(),
                python_abi="cp311",
                package_pins=("torch==0",),
            )


if __name__ == "__main__":
    unittest.main()
