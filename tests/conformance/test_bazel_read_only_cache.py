"""Real Bazel cache hits must preserve read-only shared storage."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from literate_ai.adapters.builders.bazel import _remove_bazel_directory
from literate_ai.adapters.shared_cache_config import load_shared_cache
from literate_ai.contracts.shared_cache import SharedCacheAccessMode
from tests.unit.test_shared_cache import _configuration


@unittest.skipUnless(shutil.which("bazel"), "Bazel is required for cache qualification")
class BazelReadOnlyCacheTests(unittest.TestCase):
    def test_fresh_output_base_reuses_cache_without_modifying_shared_storage(self):
        root = Path(tempfile.mkdtemp(prefix="cache-proof-")).resolve()
        self.addCleanup(_remove_bazel_directory, root)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "MODULE.bazel").write_text('module(name="cache_proof")\n')
        (workspace / "BUILD.bazel").write_text(
            'genrule(name="product", outs=["product.txt"], '
            'cmd="printf deterministic-output > $@")\n'
        )
        config_path = root / "shared-cache.json"
        environment = {
            "LITAI_CONFIG_DIR": str(root),
            "LITAI_CACHE_DIR": str(root / "cache"),
        }

        def bind(mode):
            configuration = _configuration(
                endpoint=None, credential_reference=None, bazel_mode=mode
            )
            config_path.write_text(json.dumps(configuration.to_dict()))
            return load_shared_cache(environment=environment)

        def build(label, binding):
            stage = root / label
            stage.mkdir()
            started = time.monotonic()
            arguments = binding.bazel_arguments(workspace=stage)
            copy_seconds = time.monotonic() - started
            result = subprocess.run(
                [
                    "bazel",
                    "--batch",
                    "--nosystem_rc",
                    "--nohome_rc",
                    "--noworkspace_rc",
                    f"--output_base={stage / 'output'}",
                    "build",
                    "--symlink_prefix=/",
                    *arguments,
                    "//:product",
                ],
                cwd=workspace,
                text=True,
                capture_output=True,
                timeout=180,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-4000:])
            output = next((stage / "output/execroot").rglob("product.txt"))
            return {
                "copy_seconds": copy_seconds,
                "total_seconds": time.monotonic() - started,
                "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "disk_hit": "disk cache hit" in result.stderr,
            }

        writer = bind(SharedCacheAccessMode.READ_WRITE)
        cache = writer.local_root / writer.configuration.namespace / "bazel"

        def inventory():
            return {
                str(path.relative_to(cache)): (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    path.stat().st_mtime_ns,
                )
                for path in cache.rglob("*")
                if path.is_file()
            }

        cold = build("cold", writer)
        before = inventory()
        self.assertTrue(before)
        warm = build("warm", bind(SharedCacheAccessMode.READ_ONLY))
        self.assertEqual(before, inventory())
        self.assertEqual(cold["output_sha256"], warm["output_sha256"])
        self.assertTrue(warm["disk_hit"], warm)
        print(
            json.dumps(
                {
                    "schema": "literate-ai/bazel-read-only-proof@1",
                    "cold": cold,
                    "warm": warm,
                }
            )
        )
