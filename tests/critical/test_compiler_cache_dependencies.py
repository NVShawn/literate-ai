"""Compiler cache identity includes native resolution and retained build evidence."""

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters.compiler_cache_dependencies import CompilerCacheDependencies
from literate_ai.adapters.dependencies import HostDependencyObservation
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.adapters.shared_cache_config import (
    SharedCacheConfigurationError,
    load_shared_cache,
)
from tests.support.fixtures_test_shared_cache import _configuration


def _observation(version="1"):
    return HostDependencyObservation(
        ({"bom-ref": "library", "type": "library", "version": version},),
        (("urn:literate-ai:compiler-cache-tool", "library"),),
    )


class CompilerCacheDependencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        (root / "shared-cache.json").write_text(
            json.dumps(
                _configuration(endpoint=None, credential_reference=None).to_dict()
            )
        )
        self.binding = replace(
            load_shared_cache(
                environment={
                    "LITAI_CONFIG_DIR": str(root),
                    "LITAI_CACHE_DIR": str(root / "cache"),
                }
            ),
            compiler_tool=LocalComponentToolBinding(sys.executable),
        )
        patcher = mock.patch(
            "literate_ai.adapters.compiler_cache_dependencies."
            "PortableHostDependencyObserver"
        )
        self.observer = patcher.start()
        self.addCleanup(patcher.stop)
        self.observer.return_value.observe.return_value = _observation()
        self.dependencies = CompilerCacheDependencies.observe(
            self.binding.compiler_tool, {}
        )
        self.binding = replace(self.binding, compiler_dependencies=self.dependencies)

    def test_changed_library_refuses_execution_and_changes_cache_identity(self):
        self.binding.require_compiler_dependencies({})
        self.observer.return_value.observe.return_value = _observation("2")
        with self.assertRaises(SharedCacheConfigurationError) as caught:
            self.binding.require_compiler_dependencies({})
        self.assertEqual(
            caught.exception.code, "shared_cache.compiler_dependencies_changed"
        )
        changed = CompilerCacheDependencies.observe(self.binding.compiler_tool, {})
        self.assertNotEqual(
            self.binding.identity,
            replace(self.binding, compiler_dependencies=changed).identity,
        )

    def test_unmodeled_loader_injection_refuses_before_observation(self):
        injections = {
            "linux": {"LD_PRELOAD": "/injected.so"},
            "darwin": {"DYLD_INSERT_LIBRARIES": "/injected.dylib"},
        }
        platform = "linux" if sys.platform.startswith("linux") else sys.platform
        if platform not in injections:
            self.skipTest("no modeled loader policy for this platform")
        with (
            self.subTest(platform=platform),
            self.assertRaises(SharedCacheConfigurationError),
        ):
            self.binding.require_compiler_dependencies(injections[platform])
