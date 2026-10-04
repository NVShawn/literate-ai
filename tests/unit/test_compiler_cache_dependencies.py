"""Compiler cache identity includes native resolution and retained build evidence."""

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters.compiler_cache import compiler_cache_session
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

    def test_missing_closure_is_not_an_uncached_fallback(self):
        with self.assertRaises(SharedCacheConfigurationError) as caught:
            replace(
                self.binding, compiler_dependencies=None
            ).require_compiler_dependencies({})
        self.assertEqual(
            caught.exception.code, "shared_cache.compiler_dependencies_missing"
        )

    def test_session_refuses_drift_before_start_and_after_build(self):
        self.observer.return_value.observe.return_value = _observation("changed")
        with mock.patch(
            "literate_ai.adapters.compiler_cache.subprocess.Popen"
        ) as start:
            with self.assertRaises(SharedCacheConfigurationError):
                with compiler_cache_session(
                    self.binding, environment={}, workspace=self.binding.path.parent
                ):
                    self.fail("changed dependencies must not reach compilation")
            start.assert_not_called()

        self.observer.return_value.observe.return_value = _observation()
        reached_build = False
        with mock.patch(
            "literate_ai.adapters.compiler_cache.subprocess.Popen",
            side_effect=OSError("cache server unavailable"),
        ):
            with self.assertRaises(SharedCacheConfigurationError) as caught:
                with compiler_cache_session(
                    self.binding, environment={}, workspace=self.binding.path.parent
                ):
                    reached_build = True
                    self.observer.return_value.observe.return_value = _observation(
                        "changed"
                    )
        self.assertTrue(reached_build)
        self.assertEqual(
            caught.exception.code, "shared_cache.compiler_dependencies_changed"
        )

    def test_runtime_projects_cache_dependencies_into_build_toolchain_evidence(self):
        from literate_ai.adapters.standard_project import (
            assemble_filesystem_standard_project_runtime,
        )
        from tests.support.fixtures_test_standard_project_factory import (
            _command_contracts,
            _fixture,
            _toolchain_closure,
        )

        _, execution = _fixture()
        contracts, tools = _command_contracts(execution)
        closure = _toolchain_closure(execution, contracts, tools)
        runtime = assemble_filesystem_standard_project_runtime(
            generator=lambda _prepared: None,
            object_root=self.binding.path.parent / "objects",
            toolchain_closure=closure,
            shared_cache=self.binding,
        )
        projected = runtime.toolchain_closure
        projected.require_unchanged()
        self.assertNotEqual(
            projected.record.dependency_graph_identity,
            closure.record.dependency_graph_identity,
        )
        self.assertEqual(
            len(projected.dependency_observation.components),
            len(closure.dependency_observation.components) + 1,
        )
        self.assertTrue(
            any(
                item["bom-ref"].startswith("urn:literate-ai:cache-dependency:")
                for item in projected.dependency_observation.components
            )
        )

    def test_evidence_preserves_distinct_roles_and_one_component_root(self):
        original = HostDependencyObservation(
            ({"bom-ref": "library", "type": "library", "version": "compiler-role"},),
            (("component", "library"),),
        )
        identity = self.dependencies.identity
        combined = self.dependencies.include_in(original)
        self.assertEqual(len(combined.components), 2)
        refs = {item["bom-ref"] for item in combined.components}
        self.assertEqual({a for a, _ in combined.edges if a not in refs}, {"component"})
        for item in combined.components:
            item["version"] = "mutated"
        self.assertEqual(self.dependencies.identity, identity)
        self.assertEqual(
            {
                item["version"]
                for item in self.dependencies.include_in(
                    HostDependencyObservation(
                        ({"bom-ref": "compiler"},), (("component", "compiler"),)
                    )
                ).components
                if "version" in item
            },
            {"1"},
        )

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux loader policy")
    def test_unmodeled_loader_injection_refuses_before_observation(self):
        with self.assertRaises(SharedCacheConfigurationError):
            self.binding.require_compiler_dependencies({"LD_PRELOAD": "/injected.so"})

    @unittest.skipUnless(sys.platform == "darwin", "macOS loader policy")
    def test_unmodeled_dyld_injection_refuses_before_observation(self):
        with self.assertRaises(SharedCacheConfigurationError):
            self.binding.require_compiler_dependencies(
                {"DYLD_INSERT_LIBRARIES": "/injected.dylib"}
            )
