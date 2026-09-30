"""Reviewed external directories bind mutable input bytes and reject overlays."""

import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.retained_cargo_execution_inputs import (
    read_retained_cargo_execution_inputs,
)
from literate_ai.contracts.retained_libraries import RetainedLibraryGatePolicy
from tests.unit import test_retained_cargo_execution as fixtures


class RetainedCargoExternalInputTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.RetainedCargoExecutionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        temporary = tempfile.TemporaryDirectory(prefix="external-")
        self.addCleanup(temporary.cleanup)
        self.external = Path(temporary.name).resolve()
        self.file = self.external / "native-input.txt"
        self.file.write_bytes(b"reviewed external input")
        self.original_policy = fixture.importer.gates
        self.policy = replace(
            self.original_policy, external_input_variables=("NATIVE_INPUT",)
        )
        fixture.importer = replace(fixture.importer, gates=self.policy)
        self.environment = {**fixture.environment, "NATIVE_INPUT": str(self.external)}

    def capture(self, **changes):
        return read_retained_cargo_execution_inputs(
            self.fixture.materialized,
            **{"environment": self.environment, "gate_policy": self.policy, **changes},
        )

    def execute(self, **changes):
        return self.fixture.run_execution(
            **{
                "environment": self.environment,
                "consumer_inputs": self.capture(),
                **changes,
            },
        )

    def test_optional_policy_preserves_old_wire_identity_and_binds_declarations(self):
        original = self.original_policy
        self.assertNotIn("external_input_variables", original.to_dict())
        self.assertEqual(
            RetainedLibraryGatePolicy.from_dict(original.to_dict()).identity,
            original.identity,
        )
        self.assertNotEqual(original.identity, self.policy.identity)
        self.assertEqual(
            RetainedLibraryGatePolicy.from_dict(self.policy.to_dict()), self.policy
        )
        for names in (
            ["NATIVE_INPUT"],
            ("bad-name",),
            ("lowercase",),
            ("NATIVE_INPUT",) * 2,
            (True,),
            tuple(f"INPUT_{i:03}" for i in range(65)),
        ):
            with self.subTest(names=names), self.assertRaises(ValueError):
                replace(original, external_input_variables=names)

    def test_current_declared_tree_executes_all_original_gates(self):
        captured = self.capture()
        self.assertEqual(captured.external_roots, (("NATIVE_INPUT", self.external),))
        self.assertEqual(captured.gate_policy_identity, self.policy.identity)
        self.assertEqual(len(self.execute(consumer_inputs=captured)), 3)

    @unittest.skipUnless(
        shutil.which("cargo"), "Cargo is required for native external-input proof"
    )
    def test_real_build_script_reads_the_declared_external_tree(self):
        project = self.fixture.root / "external-probe"
        project.mkdir()
        (project / "src").mkdir()
        (project / "Cargo.toml").write_text(
            '[workspace]\n[package]\nname="external-proof"\nversion="0.1.0"\nedition="2021"\n'
        )
        (project / "build.rs").write_text("""fn main() {
    let root = std::env::var("NATIVE_INPUT").unwrap();
    let input = std::path::Path::new(&root).join("native-input.txt");
    let number: i64 = std::fs::read_to_string(&input).unwrap().parse().unwrap();
    let out = std::path::Path::new(&std::env::var("OUT_DIR").unwrap()).join("value.rs");
    std::fs::write(out, format!("const VALUE: i64 = {number};")).unwrap();
    println!("cargo:rerun-if-changed={}", input.display());
}
""")
        (project / "src/lib.rs").write_text(
            'include!(concat!(env!("OUT_DIR"), "/value.rs"));\n'
            "#[test] fn external_value() { assert_eq!(VALUE, 42); }\n"
        )
        self.file.write_bytes(b"42")
        environment = {
            **os.environ,
            **self.environment,
            "CARGO_TARGET_DIR": str(self.fixture.root / "cargo-output"),
            "CARGO_NET_OFFLINE": "true",
        }

        def cargo(*args):
            result = subprocess.run(
                [shutil.which("cargo"), *args],
                cwd=project,
                env=environment,
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(
                result.returncode, 0, result.stderr.decode(errors="replace")
            )
            return result

        cargo("generate-lockfile", "--offline")
        captured = self.capture(environment=environment)
        before = captured.current_identity()
        result = cargo("test", "--locked", "--offline", "--all-targets")
        self.assertIn(b"test external_value ... ok", result.stdout)
        self.assertEqual(captured.current_identity(), before)
        self.file.write_bytes(b"43")
        with self.assertRaises(ValueError):
            captured.current_identity()

    def test_missing_policy_cannot_authorize_declared_external_inputs(self):
        with self.assertRaisesRegex(ValueError, "external-policy-mismatch"):
            self.execute(consumer_inputs=self.capture(gate_policy=None))
        self.fixture.process.assert_not_called()

    def test_source_drift_invalidates_run_without_requalifying_provider(self):
        captured = self.capture()
        before = captured.current_identity()
        provider = self.fixture.materialized._files.binding.identity
        self.file.write_bytes(b"new external source")
        with self.assertRaises(ValueError):
            captured.current_identity()
        self.assertNotEqual(self.capture().current_identity(), before)
        self.assertEqual(self.fixture.materialized._files.binding.identity, provider)
        self.assertEqual(self.fixture.importer.gates.identity, self.policy.identity)

    def test_new_external_file_during_metadata_stops_before_gate(self):
        def mutate(*args, **kwargs):
            (self.external / "added.txt").write_bytes(b"new input")
            return BoundedProcessResult(0, b'{"packages": []}', b"")

        self.fixture.process.side_effect = mutate
        with self.assertRaises(fixtures.execution.RetainedCargoExecutionError):
            self.execute()
        self.assertEqual(self.fixture.process.call_count, 1)

    def test_tool_cannot_override_reviewed_external_directory(self):
        other = self.external / "other"
        other.mkdir()
        tool = replace(self.fixture.make, environment=(("NATIVE_INPUT", str(other)),))
        with self.assertRaises(
            fixtures.execution.RetainedCargoExecutionError
        ) as failure:
            self.execute(gate_tools={"make": tool})
        self.assertIn(
            "external-input-override-refused", str(failure.exception.__cause__)
        )
        self.assertEqual(self.fixture.process.call_count, 1)

    def test_missing_relative_overlapping_and_foreign_policy_refuse(self):
        for value in (
            "",
            "relative",
            str(self.external / "missing"),
            str(self.fixture.root),
            str(self.fixture.home),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.capture(environment={**self.environment, "NATIVE_INPUT": value})
        with self.assertRaisesRegex(ValueError, "gate-policy-mismatch"):
            self.capture(
                gate_policy=replace(self.policy, importer_project_id="foreign")
            )
        duplicate = replace(
            self.policy, external_input_variables=("NATIVE_INPUT", "SECOND_INPUT")
        )
        with self.assertRaisesRegex(ValueError, "external-directory-overlap"):
            self.capture(
                gate_policy=duplicate,
                environment={**self.environment, "SECOND_INPUT": str(self.external)},
            )
        self.fixture.process.assert_not_called()
