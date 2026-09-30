"""Native compilation and per-target results, with binary-set drift refusal."""

import json
import shutil
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.builders import run_bounded_process
from literate_ai.adapters.retained_cargo_test_execution import (
    observe_retained_cargo_tests,
)
from literate_ai.contracts.cargo_workspace import CargoTargetExpectation
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.retained_cargo_tests import RetainedCargoTestTarget
from tests.unit import test_retained_cargo_test_observation as fixtures


@unittest.skipUnless(
    shutil.which("cargo"), "Cargo is required for native test execution"
)
class RetainedCargoNativeTestExecutionTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.CargoTestExecutableTests.setUpClass.__func__)
    run_cargo = classmethod(fixtures.CargoTestExecutableTests.run_cargo.__func__)

    def setUp(self):
        self.calls = []
        self.target = (
            subprocess.check_output([shutil.which("rustc"), "-vV"], text=True)
            .split("host: ", 1)[1]
            .splitlines()[0]
        )
        self.cases = {
            "test_protocol": ("duplicated_name", "panics"),
            "integration": ("duplicated_name",),
            "no-tests": ("bin_case",),
            "empty": (),
            "example": ("example_case",),
            "bench": ("bench_case",),
            "custom": (),
        }
        self.authority = SimpleNamespace(
            require_unchanged=lambda: None,
            materialized=SimpleNamespace(
                plan=SimpleNamespace(
                    workspace_root=".",
                    graph=self.expected,
                    target=self.target,
                    features=(),
                    all_features=False,
                    no_default_features=False,
                )
            ),
            importer=SimpleNamespace(project=SimpleNamespace(root=self.root)),
            inventory=SimpleNamespace(
                identity=canonical_identity("independently reviewed native cases"),
                targets=tuple(
                    RetainedCargoTestTarget(
                        ".",
                        target.name,
                        tuple(sorted(target.kinds)),
                        self.cases[target.name],
                    )
                    for target in self.expected.packages[0].targets
                    if not target.required_features
                ),
            ),
        )
        self.cargo = SimpleNamespace(command=(shutil.which("cargo"),))

    def run_observation(self, *, after_process=None):
        def run(command, tool, *, extra_guard):
            extra_guard()
            result = run_bounded_process(
                (*tool.command, *command.argv[1:]),
                cwd=self.root / command.working_directory,
                environment={
                    **self.environment,
                    **dict(getattr(tool, "environment", ())),
                },
                timeout_seconds=120,
                stdout_limit_bytes=4 * 1024 * 1024,
                stderr_limit_bytes=4 * 1024 * 1024,
                error_prefix="test.fixture",
            )
            self.calls.append((command, result))
            if after_process:
                after_process(command, result)
            extra_guard()
            if result.returncode:
                raise ValueError("native fixture process failed")
            return result

        observe_retained_cargo_tests(
            self.authority,
            self.metadata,
            self.cargo,
            offline=True,
            rustc=SimpleNamespace(command=(shutil.which("rustc"),)),
            environment=self.environment,
            run=run,
        )

    def test_custom_zero_exit_cannot_be_accepted(self):
        with self.assertRaisesRegex(ValueError, "discovery-mismatch"):
            self.run_observation()
        self.assertEqual(self.calls[0][0].step_id, "retained-test-runtime")
        self.assertTrue(any(c.step_id.endswith("-run") for c, _ in self.calls))

    def test_changed_binary_after_discovery_stops_before_execution(self):
        def mutate(command, result):
            if command.step_id.endswith("-list"):
                from pathlib import Path

                binary = Path(command.argv[0])
                binary.write_bytes(binary.read_bytes() + b"changed")

        with self.assertRaises(ValueError):
            self.run_observation(after_process=mutate)
        self.assertEqual(len(self.calls), 3)

    def test_native_graph_drift_after_discovery_stops_before_execution(self):
        changed = False

        def require_current():
            if changed:
                raise ValueError("retained.native.observation-changed")

        def after_process(command, result):
            nonlocal changed
            if command.step_id.endswith("-list"):
                changed = True

        native = SimpleNamespace(
            identity=canonical_identity("observed native fixture graph"),
            require_unchanged=require_current,
        )
        with (
            patch(
                "literate_ai.adapters.retained_cargo_test_execution."
                "observe_retained_native_dependencies",
                return_value=native,
            ) as observe_native,
            self.assertRaisesRegex(ValueError, "retained.native.observation-changed"),
        ):
            self.run_observation(after_process=after_process)
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(self.calls[-1][0].step_id.endswith("-list"))
        selected = observe_native.call_args.kwargs["macos_environment"]
        if sys.platform == "darwin":
            self.assertEqual(selected["CARGO_MANIFEST_DIR"], str(self.root))
            self.assertEqual(selected["PATH"], self.environment["PATH"])
            compiler_libdir = self.calls[0][1].stdout.decode().strip()
            self.assertIn(
                compiler_libdir, selected["DYLD_FALLBACK_LIBRARY_PATH"].split(":")
            )
        else:
            self.assertIsNone(selected)
        linux_selected = observe_native.call_args.kwargs["linux_environment"]
        if sys.platform.startswith("linux"):
            self.assertEqual(linux_selected["CARGO_MANIFEST_DIR"], str(self.root))
            self.assertEqual(linux_selected["PATH"], self.environment["PATH"])
            compiler_libdir = self.calls[0][1].stdout.decode().strip()
            self.assertIn(compiler_libdir, linux_selected["LD_LIBRARY_PATH"].split(":"))
        else:
            self.assertIsNone(linux_selected)

    def test_compilation_has_fresh_owned_output_and_reviewed_selection(self):
        outputs = []
        for _ in range(2):
            with self.assertRaises(ValueError):
                self.run_observation()
            command, compiled = next(
                (c, r)
                for c, r in reversed(self.calls)
                if c.step_id == "retained-test-compile"
            )
            argv = command.argv
            outputs.append(argv[argv.index("--target-dir") + 1])
            self.assertEqual(argv[argv.index("--target") + 1], self.target)
            for flag in (
                "--workspace",
                "--all-targets",
                "--no-run",
                "--locked",
                "--offline",
            ):
                self.assertIn(flag, argv)
            artifacts = [json.loads(line) for line in compiled.stdout.splitlines()]
            self.assertTrue(
                all(
                    not a["fresh"]
                    for a in artifacts
                    if a.get("reason") == "compiler-artifact"
                )
            )
        self.assertNotEqual(*outputs)

    def test_other_binary_replacement_stops_the_current_target(self):
        def mutate(command, result):
            if command.step_id.endswith("-list"):
                records = [
                    json.loads(line)
                    for line in next(
                        r for c, r in self.calls if c.step_id == "retained-test-compile"
                    ).stdout.splitlines()
                ]
                other = next(
                    Path(record["executable"])
                    for record in records
                    if record.get("executable")
                    and record["executable"] != command.argv[0]
                )
                content = other.read_bytes()
                mode = other.stat().st_mode
                other.unlink()
                other.write_bytes(content)
                other.chmod(mode)

        with self.assertRaisesRegex(ValueError, "binary-changed|runtime.files-changed"):
            self.run_observation(after_process=mutate)
        self.assertEqual(len(self.calls), 3)

    def test_all_reviewed_targets_pass_with_standard_harness(self):
        self.standard_harness_observation()

    def test_generated_data_drift_stops_before_test_execution(self):
        def mutate(command, result):
            if command.step_id.endswith("-list"):
                compiled = next(
                    r for c, r in self.calls if c.step_id == "retained-test-compile"
                )
                record = next(
                    record
                    for line in compiled.stdout.splitlines()
                    if (record := json.loads(line)).get("reason")
                    == "build-script-executed"
                )
                (Path(record["out_dir"]) / "nested" / "data.txt").write_text("changed")

        with self.assertRaises(ValueError):
            self.standard_harness_observation(after_process=mutate)
        self.assertEqual(len(self.calls), 3)

    def standard_harness_observation(self, *, after_process=None):
        # Give the custom target a standard authored harness for this one run.
        manifest = self.root / "Cargo.toml"
        original = manifest.read_text()
        source = self.root / "custom.rs"
        old_source = source.read_text()
        library = self.root / "src/lib.rs"
        old_library = library.read_text()
        build_script = self.root / "build.rs"
        try:
            self.environment = {**self.environment, "RUSTFLAGS": "-C prefer-dynamic"}
            manifest.write_text(original.replace("harness=false", "harness=true"))
            source.write_text("#[test] fn custom_case() {}\n")
            build_script.write_text(
                "fn main() { "
                'println!("cargo:rustc-env=RETAINED_CASE=package=value"); '
                'println!("cargo:rustc-env=RETAINED_EMPTY="); '
                'println!("cargo:rustc-env=CARGO_MANIFEST_DIR={}", '
                'std::env::var("CARGO_MANIFEST_DIR").unwrap()); '
                "let out = std::path::PathBuf::from("
                'std::env::var("OUT_DIR").unwrap()); '
                'std::fs::create_dir_all(out.join("nested")).unwrap(); '
                'std::fs::write(out.join("nested/data.txt"), '
                '"generated data").unwrap(); }\n'
            )
            library.write_text(
                old_library.replace(
                    "fn duplicated_name() {}",
                    "fn duplicated_name() { assert_eq!("
                    'std::env::var("RETAINED_CASE").unwrap(), '
                    'env!("RETAINED_CASE")); '
                    'assert_eq!(std::env::var("RETAINED_EMPTY").unwrap(), ""); '
                    "assert_eq!(std::fs::read_to_string(concat!("
                    'env!("OUT_DIR"), "/nested/data.txt")).unwrap(), '
                    '"generated data"); }',
                )
            )
            metadata = json.loads(
                self.run_cargo(
                    "metadata", "--locked", "--offline", "--format-version=1"
                ).stdout
            )
            self.authority.inventory.targets = tuple(
                replace(t, cases=("custom_case",)) if t.target_name == "custom" else t
                for t in self.authority.inventory.targets
            )
            graph = self.expected
            package = graph.packages[0]
            self.authority.materialized.plan.graph = replace(
                graph,
                packages=(
                    replace(
                        package,
                        targets=(
                            *package.targets,
                            CargoTargetExpectation(
                                "build-script-build",
                                ("custom-build",),
                                ("bin",),
                                "build.rs",
                                "2021",
                                False,
                                False,
                                (),
                            ),
                        ),
                    ),
                ),
            )
            with patch.object(self, "metadata", metadata):
                self.run_observation(after_process=after_process)
            self.assertEqual(len(self.calls), 2 + 2 * 7)
            self.assertTrue(all(result.returncode == 0 for _, result in self.calls))
        finally:
            manifest.write_text(original)
            source.write_text(old_source)
            library.write_text(old_library)
            build_script.unlink(missing_ok=True)

    def test_runtime_file_drift_stops_before_test_execution(self):
        runtime_file = None

        def mutate(command, result):
            nonlocal runtime_file
            if command.step_id == "retained-test-compile":
                output = Path(command.argv[command.argv.index("--target-dir") + 1])
                runtime_file = output / self.target / "debug" / "runtime-input"
                runtime_file.write_bytes(b"captured runtime input")
            elif command.step_id.endswith("-list"):
                runtime_file.write_bytes(b"changed runtime input")

        with self.assertRaises(ValueError):
            self.run_observation(after_process=mutate)
        self.assertEqual(len(self.calls), 3)
