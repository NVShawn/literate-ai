"""Shared test fixtures extracted from test_retained_cargo_execution."""

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters import retained_cargo_execution as execution
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.adapters.retained_cargo_current import RetainedCargoImporterAuthority
from literate_ai.adapters.retained_cargo_execution_inputs import (
    read_retained_cargo_execution_inputs,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.retained_libraries import RetainedLibraryGatePolicy
from literate_ai.projects import PinnedInputClosure
from tests.support import fixtures_test_retained_cargo_materialization as fixtures


class RetainedCargoExecutionTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.RetainedCargoMaterializationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.materialized = fixture.run_materializer()
        self.root = fixture.root
        temporary = tempfile.TemporaryDirectory(prefix="ch-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.environment = {
            "CARGO_HOME": str(self.home),
            "OBJ_DIR": "pkgs",
            "BUILD_DIR": "build",
        }
        self.consumer_source = self.root / "consumer.rs"
        self.consumer_source.write_text("// captured consumer\n")

        def measured(identity):
            return LocalComponentToolBinding.from_observed_toolchain(
                SimpleNamespace(
                    command=(sys.executable,),
                    identity=identity.uri,
                    require_unchanged=lambda: None,
                )
            )

        self.cargo = measured(self.materialized.plan.cargo_identity)
        self.rustc = measured(self.materialized.plan.rustc_identity)
        self.make = measured(canonical_identity("measured make fixture"))
        self.importer = RetainedCargoImporterAuthority(
            self.materialized._files.project,
            fixture.fixture.binding.identity,
            fixture.fixture.binding.source_store_id,
            RetainedLibraryGatePolicy(
                importer_project_id=self.materialized._files.project.definition.project_id,
                commands=self.materialized.plan.gates,
                toolchains=(self.make.toolchain_identity,),
            ),
            PinnedInputClosure(),
        )
        self.processes = []
        self.graphs = []

        def process(argv, **kwargs):
            self.processes.append((argv, kwargs))
            return BoundedProcessResult(0, b'{"packages": []}', b"")

        self.process = patch.object(
            execution, "run_bounded_process", side_effect=process
        ).start()
        self.graph = patch.object(
            execution,
            "verify_cargo_workspace_graph",
            side_effect=lambda *a, **kw: self.graphs.append(kw),
        ).start()
        self.addCleanup(patch.stopall)

    def run_execution(self, **changes):
        environment = changes.get("environment", self.environment)
        return execution.execute_retained_cargo_consumer(
            self.materialized,
            self.importer,
            **{
                "cargo": self.cargo,
                "rustc": self.rustc,
                "gate_tools": {"make": self.make},
                "environment": environment,
                "consumer_inputs": read_retained_cargo_execution_inputs(
                    self.materialized, environment=environment
                ),
                "allow_host_execution": True,
                **changes,
            },
        )

    def test_metadata_brackets_every_exact_reviewed_gate(self):
        observations = self.run_execution()
        self.assertEqual(
            [o.command.step_id for o in observations],
            [
                "retained-cargo-metadata",
                "existing-full-tests",
                "retained-cargo-metadata",
            ],
        )
        self.assertEqual(len(self.graphs), 2)
        self.assertTrue(
            all(g["expected"] == self.materialized.plan.graph for g in self.graphs)
        )
        for argv, kwargs in self.processes:
            self.assertIn(argv[0], (self.cargo.executable, self.make.executable))
            self.assertEqual(kwargs["environment"]["RUSTC"], self.rustc.executable)
            self.assertEqual(
                kwargs["environment"]["CARGO_TARGET_DIR"],
                str(self.root / "cargo-output"),
            )
        self.assertIn("--locked", self.processes[0][0])
        self.assertIn("--offline", self.processes[0][0])
        self.assertEqual(self.processes[1][0], (*self.make.command, "test"))
        self.materialized.require_unchanged()

    def test_no_execution_without_authorization_or_complete_tool_bindings(self):
        for changes in (
            {"allow_host_execution": False},
            {"gate_tools": {}},
            {"timeout_seconds": float("inf")},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.run_execution(**changes)
        self.process.assert_not_called()

    def test_metadata_mismatch_stops_before_any_gate(self):
        self.graph.side_effect = ValueError("wrong native graph")
        with self.assertRaises(execution.RetainedCargoExecutionError) as failure:
            self.run_execution()
        self.assertEqual(len(failure.exception.observations), 1)
        self.assertEqual(self.process.call_count, 1)

    def test_failed_gate_preserves_output_and_never_runs_final_metadata(self):
        self.process.side_effect = [
            BoundedProcessResult(0, b'{"packages": []}', b""),
            BoundedProcessResult(1, b"failed-test", b"detail"),
        ]
        with self.assertRaises(execution.RetainedCargoExecutionError) as failure:
            self.run_execution()
        self.assertEqual(self.process.call_count, 2)
        self.assertEqual(
            failure.exception.observations[-1].result.stdout, b"failed-test"
        )

    def test_consumer_drift_after_process_refuses_subsequent_gate(self):
        def changed(*args, **kwargs):
            self.consumer_source.write_text("// changed consumer\n")
            return BoundedProcessResult(0, b'{"packages": []}', b"")

        self.process.side_effect = changed
        with self.assertRaises(execution.RetainedCargoExecutionError):
            self.run_execution()
        self.assertEqual(self.process.call_count, 1)

    def test_compiler_and_output_overrides_refuse_before_process(self):
        for environment in (
            {"RUSTC": "/foreign/compiler"},
            {"CARGO_TARGET_DIR": "/foreign/output"},
        ):
            with (
                self.subTest(environment=environment),
                self.assertRaises(ValueError),
            ):
                self.run_execution(environment={**self.environment, **environment})
        self.process.assert_not_called()

    def test_uncaptured_dependency_path_stops_before_gate(self):
        self.process.side_effect = lambda *a, **kw: BoundedProcessResult(
            0,
            json.dumps(
                {
                    "packages": [
                        {
                            "manifest_path": str(self.home / "outside/Cargo.toml"),
                            "targets": [],
                        }
                    ]
                }
            ).encode(),
            b"",
        )
        with self.assertRaises(execution.RetainedCargoExecutionError) as failure:
            self.run_execution()
        self.assertEqual(self.process.call_count, 1)
        self.assertEqual(len(failure.exception.observations), 1)

    def test_asserted_consumer_identity_is_not_captured_input_authority(self):
        with self.assertRaisesRegex(ValueError, "configuration-invalid"):
            self.run_execution(consumer_inputs=lambda: canonical_identity("claimed"))
        self.process.assert_not_called()

    def test_environment_changes_require_new_input_capture(self):
        inputs = read_retained_cargo_execution_inputs(
            self.materialized, environment=self.environment
        )
        with self.assertRaisesRegex(ValueError, "input-environment-mismatch"):
            self.run_execution(
                consumer_inputs=inputs,
                environment={**self.environment, "RUSTFLAGS": "--cfg=changed"},
            )
        self.process.assert_not_called()

    def test_offline_refuses_network_enabled_gate(self):
        plan = replace(
            self.materialized.plan,
            gates=(replace(self.materialized.plan.gates[0], network=True),),
        )
        self.materialized = replace(self.materialized, plan=plan)
        self.importer = replace(
            self.importer, gates=replace(self.importer.gates, commands=plan.gates)
        )
        with self.assertRaisesRegex(ValueError, "network-gate-refused"):
            self.run_execution()
        self.process.assert_not_called()

    def test_duplicate_metadata_fields_refuse_before_gate(self):
        self.process.side_effect = lambda *a, **kw: BoundedProcessResult(
            0, b'{"version":1,"version":1}', b""
        )
        with self.assertRaises(execution.RetainedCargoExecutionError):
            self.run_execution()
        self.assertEqual(self.process.call_count, 1)
        self.graph.assert_not_called()

    def test_foreign_addition_during_process_is_preserved_and_stops_execution(self):
        foreign = self.materialized.packages[0].root / "foreign"

        def mutate(*a, **kw):
            foreign.write_bytes(b"keep")
            return BoundedProcessResult(0, b'{"packages": []}', b"")

        self.process.side_effect = mutate
        with self.assertRaises(execution.RetainedCargoExecutionError) as failure:
            self.run_execution()
        self.assertEqual(self.process.call_count, 1)
        self.assertEqual(len(failure.exception.observations), 1)
        self.assertEqual(foreign.read_bytes(), b"keep")

    def test_symlinked_output_directory_refuses_before_process(self):
        outside = self.root / "foreign-output"
        outside.mkdir()
        (outside / "keep").write_bytes(b"keep")
        try:
            (self.root / "cargo-output").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("host does not permit symbolic links")
        with self.assertRaises(ValueError):
            self.run_execution()
        self.process.assert_not_called()
        self.assertEqual((outside / "keep").read_bytes(), b"keep")

    def test_asserted_tool_identity_without_observer_custody_refuses(self):
        for name, original in (
            ("cargo", self.cargo),
            ("rustc", self.rustc),
            ("make", self.make),
        ):
            candidate = LocalComponentToolBinding(
                sys.executable, authority_identity=original.toolchain_identity
            )
            changes = (
                {name: candidate}
                if name != "make"
                else {"gate_tools": {"make": candidate}}
            )
            with (
                self.subTest(tool=name),
                self.assertRaisesRegex(ValueError, "measured-tool-custody-required"),
            ):
                self.run_execution(**changes)
        self.process.assert_not_called()

    def test_observer_drift_after_launch_preserves_observation_and_stops(self):
        changed = False

        def observer_guard():
            if changed:
                raise ValueError("native observer drift")

        cargo = LocalComponentToolBinding.from_observed_toolchain(
            SimpleNamespace(
                command=(sys.executable,),
                identity=self.materialized.plan.cargo_identity.uri,
                require_unchanged=observer_guard,
            )
        )

        def process(*args, **kwargs):
            nonlocal changed
            changed = True
            return BoundedProcessResult(0, b'{"packages": []}', b"")

        self.process.side_effect = process
        with self.assertRaises(execution.RetainedCargoExecutionError) as failure:
            self.run_execution(cargo=cargo)
        self.assertEqual(self.process.call_count, 1)
        self.assertEqual(len(failure.exception.observations), 1)
