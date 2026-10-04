"""Real host-toolchain closure conformance for the Standard project boundary."""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.builders import discover_python_toolchain
from literate_ai.adapters.dependencies import PortableHostDependencyObserver
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.adapters.standard_project import project_standard_toolchain_closure
from literate_ai.contracts import (
    ComponentCommandPhase,
    ComponentCommandToolBinding,
    canonical_identity,
)
from tests.support.fixtures_test_component_node_generation_preparation import _fixture
from tests.support.fixtures_test_standard_project_factory import _command_contracts


class StandardToolchainClosureConformanceTests(unittest.TestCase):
    def test_real_python_toolchain_has_recursive_host_closure(self) -> None:
        _snapshot, execution = _fixture()
        original_contracts, _original_bindings = _command_contracts(execution)
        toolchain = discover_python_toolchain()
        binding = LocalComponentToolBinding.from_observed_toolchain(toolchain)
        contracts = tuple(
            replace(
                contract,
                build_system_toolchain_identity=binding.toolchain_identity,
                language_compiler_identity=binding.toolchain_identity,
                language_runtime_identity=binding.toolchain_identity,
                tool_bindings=tuple(
                    ComponentCommandToolBinding(phase, binding.toolchain_identity)
                    for phase in ComponentCommandPhase
                ),
            )
            for contract in original_contracts
        )
        with tempfile.TemporaryDirectory() as directory:
            observation = PortableHostDependencyObserver(
                toolchain_commands=(toolchain.command,),
                lifecycle_commands=(),
            ).observe(
                {"artifact_path": str(Path(directory))},
                root_ref="component:standard-toolchain-conformance",
            )

        closure = project_standard_toolchain_closure(
            execution,
            contracts=contracts,
            tool_bindings=(binding,),
            dependency_observation=observation,
            observer_identity=canonical_identity(
                {
                    "observer": "portable-host-dependency-observer@1",
                    "platform": sys.platform,
                }
            ),
        )

        self.assertGreater(len(observation.components), 0)
        self.assertGreater(len(observation.edges), 0)
        self.assertEqual(
            closure.record.toolchain_identities,
            (binding.toolchain_identity,),
        )
        self.assertTrue(
            closure.record.dependency_graph_identity.uri.startswith("sha256:")
        )
        closure.require_unchanged()


if __name__ == "__main__":
    unittest.main()
