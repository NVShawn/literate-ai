"""Private cache binding and credential custody before production builds."""

from __future__ import annotations

import os
import shlex
import tempfile
import time
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

    def test_missing_default_is_opt_out_but_explicit_missing_is_an_error(self):
        self.assertIsNone(load_shared_cache(environment=self.environment))
        self.environment["LITAI_SHARED_CACHE_CONFIG"] = str(self.path)
        with self.assertRaisesRegex(SharedCacheConfigurationError, "is missing"):
            load_shared_cache(environment=self.environment)

    def test_configuration_selects_private_root_and_drift_is_refused(self):
        binding = self.bind()
        self.assertEqual(
            binding.local_root, self.root / "cache" / "shared" / "team-cache"
        )
        self.assertFalse(binding.local_root.exists())
        self.assertIn("--remote_verify_downloads=true", binding.bazel_arguments())
        self.path.write_bytes(self.path.read_bytes() + b" ")
        with self.assertRaisesRegex(SharedCacheConfigurationError, "changed"):
            binding.bazel_arguments()

    def test_duplicate_keys_and_symlink_configuration_are_refused(self):
        self.path.write_text('{"schema": "one", "schema": "two"}')
        with self.assertRaisesRegex(SharedCacheConfigurationError, "invalid private"):
            load_shared_cache(environment=self.environment)
        self.path.unlink()
        target = self.root / "target.json"
        target.write_bytes(canonical_json_bytes(self.configuration.to_dict()))
        try:
            self.path.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(SharedCacheConfigurationError, "unsafe"):
            load_shared_cache(environment=self.environment)

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

    def test_invalid_credentials_are_refused_when_binding_before_build(self):
        configuration = replace(
            self.configuration,
            endpoint="https://cache.example.invalid",
            credential_reference="env:AUTH_VALUE",
        )
        for token in ("", "secret\nheader", "contains space"):
            with self.subTest(token=token):
                self.environment["AUTH_VALUE"] = token
                with self.assertRaisesRegex(
                    SharedCacheConfigurationError, "credential is absent or invalid"
                ):
                    self.bind(configuration)
        with self.assertRaisesRegex(SharedCacheConfigurationError, "explicit env:NAME"):
            self.bind(replace(configuration, credential_reference="keyring:cache"))

    def test_unsafe_cache_parent_is_refused(self):
        binding = self.bind()
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (self.root / "cache").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(SharedCacheConfigurationError, "root is unsafe"):
            binding.bazel_arguments()

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

    def test_read_only_bazel_uses_an_independent_disposable_copy(self):
        binding = self.bind(
            _configuration(
                endpoint=None,
                credential_reference=None,
                bazel_mode=SharedCacheAccessMode.READ_ONLY,
            )
        )
        source = binding.local_root / binding.configuration.namespace / "bazel"
        (source / "cas").mkdir(parents=True)
        original = source / "cas" / "object"
        original.write_bytes(b"verified separately by Bazel")
        before = original.stat()
        expired = source / "cas" / "expired"
        expired.write_bytes(b"expired")
        old = time.time() - binding.configuration.retention_seconds - 60
        os.utime(expired, (old, old))
        arguments = binding.bazel_arguments(workspace=self.root)
        view = Path(
            next(
                arg.split("=", 1)[1]
                for arg in arguments
                if arg.startswith("--disk_cache=")
            )
        )
        self.assertEqual((view / "cas/object").read_bytes(), original.read_bytes())
        self.assertFalse((view / "cas/expired").exists())
        (view / "cas/object").write_bytes(b"Bazel is allowed to overwrite its copy")
        (view / "new-entry").write_bytes(b"new result")
        self.assertEqual(original.read_bytes(), b"verified separately by Bazel")
        self.assertEqual(original.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertFalse((source / "new-entry").exists())
        self.assertTrue(expired.exists())

    def test_over_budget_read_only_cache_becomes_an_empty_view(self):
        configuration = replace(
            _configuration(endpoint=None, credential_reference=None), maximum_bytes=1
        )
        binding = self.bind(configuration)
        source = binding.local_root / configuration.namespace / "bazel"
        source.mkdir(parents=True)
        (source / "large").write_bytes(b"too large")
        binding.bazel_arguments(workspace=self.root)
        self.assertEqual(list((self.root / "cache-view").iterdir()), [])
        self.assertEqual((source / "large").read_bytes(), b"too large")
