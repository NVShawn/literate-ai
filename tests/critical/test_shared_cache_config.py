"""Private cache binding and credential custody before production builds."""

from __future__ import annotations

import os
import shlex
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.shared_cache_config import (
    SharedCacheConfigurationError,
    load_shared_cache,
)
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.shared_cache import SharedCacheAccessMode
from tests.support.fixtures_test_shared_cache import _configuration


class SharedCacheConfigurationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.path = self.root / "shared-cache.json"
        self.environment = dict(os.environ) | {
            "LITAI_CONFIG_DIR": str(self.root),
            "LITAI_CACHE_DIR": str(self.root / "cache"),
        }
        self.environment.pop("LITAI_SHARED_CACHE_CONFIG", None)
        self.configuration = _configuration(
            endpoint=None,
            credential_reference=None,
            bazel_mode=SharedCacheAccessMode.READ_WRITE,
        )

    def bind(self, configuration=None):
        self.path.write_bytes(
            canonical_json_bytes((configuration or self.configuration).to_dict())
        )
        return load_shared_cache(environment=self.environment)

    def test_private_credentials_are_temporary_and_absent_from_arguments(self):
        configuration = replace(
            self.configuration,
            endpoint="https://cache.example.invalid",
            credential_reference="env:AUTH_VALUE",
        )
        self.environment["AUTH_VALUE"] = "private-token"
        binding = self.bind(configuration)
        self.assertNotIn("private-token", repr(binding))
        with binding.bazel_credentials(self.root) as startup:
            self.assertNotIn("private-token", str(startup))
            path = Path(startup[0].split("=", 1)[1])
            self.assertEqual(
                shlex.split(path.read_text()),
                ["build", "--remote_cache_header=Authorization=Bearer private-token"],
            )
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(path.exists())
        self.assertNotIn("private-token", str(binding.bazel_arguments()))

    def test_credential_cleanup_on_invocation_failure(self):
        self.environment["AUTH_VALUE"] = "private-token"
        binding = self.bind(
            replace(
                self.configuration,
                endpoint="https://cache.example.invalid",
                credential_reference="env:AUTH_VALUE",
            )
        )
        with self.assertRaisesRegex(RuntimeError, "failed"):
            with binding.bazel_credentials(self.root) as startup:
                path = Path(startup[0].split("=", 1)[1])
                raise RuntimeError("failed")
        self.assertFalse(path.exists())

    def test_symlink_namespace_cannot_redirect_a_writable_cache(self):
        binding = self.bind()
        namespace = binding.local_root / binding.configuration.namespace
        namespace.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (namespace / "bazel").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(SharedCacheConfigurationError, "root is unsafe"):
            binding.bazel_arguments()
