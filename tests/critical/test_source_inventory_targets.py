"""Vendor attribution is not a CUDA target requirement; real evidence survives."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from literate_ai.source_to_specification.inventory import inventory_source
from literate_ai.source_to_specification.skills import (
    builtin_skill_set,
    load_builtin_skill_catalog,
)
from literate_ai.source_to_specification.static_workflow import derive_static_checkout


class SourceInventoryTargetTests(unittest.TestCase):
    def test_vendor_attribution_does_not_imply_cuda_or_mutate_source(self):
        cases = (
            (
                "portable.rs",
                "// SPDX-FileCopyrightText: Copyright (c) 2026 "
                "NVIDIA CORPORATION & AFFILIATES.\n",
            ),
            (
                "portable.rs",
                "/* SPDX-FileCopyrightText: Copyright (c) 2026 "
                "NVIDIA CORPORATION & AFFILIATES. */\n",
            ),
            ("portable.rs", 'const VENDOR: &str = "NVIDIA";\n'),
            ("nvidia.rs", "// Portable vendor integration without an accelerator.\n"),
        )
        catalog = load_builtin_skill_catalog()
        for name, attribution in cases:
            with (
                self.subTest(name=name, attribution=attribution),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                source = root / name
                payload = (
                    attribution + "pub fn identity(value: u64) -> u64 { value }\n"
                ).encode()
                source.write_bytes(payload)
                before = hashlib.sha256(payload).digest()
                inventory = inventory_source(root)
                self.assertNotIn(
                    "cuda",
                    {
                        signal.flavor_id
                        for signal in inventory.entries[0].flavor_signals
                    },
                )
                derived = derive_static_checkout(
                    source=root,
                    inventory=inventory,
                    origin_attestation_id="fixture-origin",
                    skill_set=builtin_skill_set(catalog, languages=("rust",)),
                    skill_catalog=catalog,
                )
                self.assertNotIn(
                    "cuda", {draft["flavor_id"] for draft in derived.flavor_drafts}
                )
                self.assertTrue(derived.result.observations)
                self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), before)

    def test_cuda_source_compiler_headers_and_apis_remain_detectable(self):
        cases = (
            ("kernel.cu", "__global__ void add() {}\n"),
            ("kernel.cuh", "__device__ int value();\n"),
            ("build.sh", "nvcc -c kernel.cc\n"),
            ("host.cpp", "#include <cuda.h>\n"),
            ("host.cpp", "#include <cuda_runtime.h>\n"),
            ("host.cpp", "#include <cuda_runtime_api.h>\n"),
            ("host.cpp", "void allocate() { cudaMalloc(&buffer, 64); }\n"),
            ("host.cpp", "void initialize() { cuInit(0); }\n"),
            ("Cargo.toml", "[features]\ncuda = []\n"),
        )
        for name, text in cases:
            with (
                self.subTest(name=name, text=text),
                tempfile.TemporaryDirectory() as tmp,
            ):
                path = Path(tmp) / name
                payload = text.encode()
                path.write_bytes(payload)
                inventory = inventory_source(path)
                self.assertIn(
                    "cuda",
                    {
                        signal.flavor_id
                        for signal in inventory.entries[0].flavor_signals
                    },
                )
                self.assertEqual(path.read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
