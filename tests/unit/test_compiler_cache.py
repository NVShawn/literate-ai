"""Cache observations cannot invent hits or expose uncontrolled configuration."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters.compiler_cache import (
    _counter,
    _startup_port,
    compiler_cache_session,
    validate_compiler_cache_observation,
)
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.adapters.shared_cache_config import BoundSharedCache, load_shared_cache
from literate_ai.contracts.identity import canonical_identity
from tests.support.fixtures_test_shared_cache import _configuration


class CompilerCacheTests(unittest.TestCase):
    def test_startup_acknowledgement_admits_only_the_owned_loopback_port(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ready"
            address = b"127.0.0.1:43210"
            payload = (
                (0).to_bytes(4, "little") + len(address).to_bytes(8, "little") + address
            )
            framed = len(payload).to_bytes(4, "big") + payload
            path.write_bytes(framed)
            self.assertEqual(_startup_port(path), 43210)
            for rejected in (
                b"",
                framed[:-1],
                framed + b"extra",
                framed.replace(b"127.0.0.1", b"192.0.0.1"),
                (4).to_bytes(4, "big") + (1).to_bytes(4, "little"),
            ):
                path.write_bytes(rejected)
                self.assertIsNone(_startup_port(path))

    def test_language_and_detailed_counts_are_not_added_twice(self):
        self.assertEqual(
            _counter(
                {
                    "counts": {"C/C++": 2, "Rust": 1},
                    "adv_counts": {"c [clang]": 2, "rust": 1},
                }
            ),
            3,
        )
        self.assertEqual(_counter(True), 0)
        self.assertEqual(_counter(-1), 0)

    def test_observations_reject_invalid_statistics_and_unknown_fields(self):
        identity = canonical_identity({"fixture": "compiler-cache"}).uri
        observation = {
            "schema": "literate-ai/compiler-cache-observation@1",
            "configuration_identity": identity,
            "tool_identity": identity,
            "available": True,
            "cache_hits": 1,
            "cache_misses": 0,
            "compile_requests": 1,
        }
        self.assertEqual(validate_compiler_cache_observation(observation), observation)
        for patch in (
            {"cache_hits": -1},
            {"cache_hits": True},
            {"available": False},
            {"secret": "must not be accepted"},
            {"statistics_unavailable": True},
        ):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_compiler_cache_observation(observation | patch)

    def test_unavailable_server_falls_back_without_ambient_cache_or_wrapper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "shared-cache.json").write_text(
                json.dumps(
                    _configuration(endpoint=None, credential_reference=None).to_dict()
                )
            )
            binding = load_shared_cache(
                environment={
                    "LITAI_CONFIG_DIR": str(root),
                    "LITAI_CACHE_DIR": str(root / "cache"),
                }
            )
            binding = replace(
                binding, compiler_tool=LocalComponentToolBinding(sys.executable)
            )
            with (
                mock.patch(
                    "literate_ai.adapters.compiler_cache.subprocess.Popen",
                    side_effect=OSError("unavailable"),
                ),
                mock.patch.object(BoundSharedCache, "require_compiler_dependencies"),
            ):
                with compiler_cache_session(
                    binding,
                    workspace=root,
                    environment={
                        "SCCACHE_CONF": "unrelated",
                        "SCCACHE_WEBDAV_TOKEN": "secret",
                        "RUSTC_WRAPPER": "unrelated",
                        "PATH": "retained",
                    },
                ) as session:
                    self.assertFalse(session.observation["available"])
                    self.assertEqual(session.environment["RUSTC_WRAPPER"], "")
                    self.assertEqual(session.environment["PATH"], "retained")
                    self.assertFalse(
                        any(key.startswith("SCCACHE_") for key in session.environment)
                    )
            validate_compiler_cache_observation(session.observation)
