"""Native compilation and per-target results, with binary-set drift refusal."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.builders import run_bounded_process
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.retained_cargo_test_execution import (
    observe_retained_cargo_tests,
)
from literate_ai.contracts.cargo_workspace import (
    CargoPackageExpectation,
    CargoTargetExpectation,
    CargoWorkspaceExpectation,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.retained_cargo_tests import RetainedCargoTestTarget
from tests.support.loader_environment import without_empty_loader_entries

FILES = {
    "Cargo.toml": """[workspace]
[package]
name="test-protocol"
version="0.1.0"
edition="2021"
[lib]
name="test_protocol"
[features]
extra=[]
[[bin]]
name="no-tests"
path="bin.rs"
test=false
[[bin]]
name="empty"
path="empty.rs"
[[example]]
name="example"
path="example.rs"
[[example]]
name="gated"
path="gated.rs"
required-features=["extra"]
[[bench]]
name="bench"
path="bench.rs"
[[test]]
name="custom"
path="custom.rs"
harness=false
[profile.test]
debug=0
""",
    "src/lib.rs": (
        "#[test] fn duplicated_name() {}\n"
        '#[test] #[should_panic] fn panics() { panic!("expected"); }\n'
    ),
    "tests/integration.rs": "#[test] fn duplicated_name() {}\n",
    "bin.rs": "fn main() {}\n#[test] fn bin_case() {}\n",
    "empty.rs": "fn main() {}\n",
    "example.rs": "fn main() {}\n#[test] fn example_case() {}\n",
    "gated.rs": "fn main() {}\n#[test] fn gated_case() {}\n",
    "bench.rs": "#[test] fn bench_case() {}\n",
    "custom.rs": 'fn main() { println!("not test evidence"); }\n',
}


@unittest.skipUnless(
    shutil.which("cargo"), "Cargo is required for native test execution"
)
class RetainedCargoNativeTestExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Only lockfile and metadata are prepared here: the product compiles
        # into its own fresh output, so pre-compiling the fixture is wasted.
        temporary = tempfile.TemporaryDirectory(prefix="ct-")
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name).resolve()
        for name, text in FILES.items():
            path = cls.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        cls.environment = {
            **without_empty_loader_entries(os.environ),
            "CARGO_HOME": str(cls.root / "home"),
            "CARGO_TARGET_DIR": str(cls.root / "out"),
            "CARGO_NET_OFFLINE": "true",
            "CARGO_INCREMENTAL": "0",
        }
        cls.run_cargo("generate-lockfile", "--offline")
        cls.metadata = json.loads(
            cls.run_cargo(
                "metadata", "--locked", "--offline", "--format-version=1"
            ).stdout
        )
        targets = tuple(
            CargoTargetExpectation(
                name,
                (kind,),
                ("lib",) if kind == "lib" else ("bin",),
                source,
                "2021",
                test,
                kind == "lib",
                required,
            )
            for name, kind, source, test, required in (
                ("test_protocol", "lib", "src/lib.rs", True, ()),
                ("integration", "test", "tests/integration.rs", True, ()),
                ("custom", "test", "custom.rs", True, ()),
                ("no-tests", "bin", "bin.rs", False, ()),
                ("empty", "bin", "empty.rs", True, ()),
                ("example", "example", "example.rs", False, ()),
                ("gated", "example", "gated.rs", False, ("extra",)),
                ("bench", "bench", "bench.rs", False, ()),
            )
        )
        cls.expected = CargoWorkspaceExpectation(
            (CargoPackageExpectation(".", "test-protocol", "0.1.0", (), targets),),
            (),
            (".",),
            (".",),
            ".",
            "out",
        )

    @classmethod
    def run_cargo(cls, *arguments):
        result = subprocess.run(
            [shutil.which("cargo"), *arguments],
            cwd=cls.root,
            env=cls.environment,
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr.decode(errors="replace"))
        return BoundedProcessResult(result.returncode, result.stdout, result.stderr)

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

    def test_changed_binary_after_discovery_stops_before_execution(self):
        def mutate(command, result):
            if command.step_id.endswith("-list"):
                binary = Path(command.argv[0])
                binary.write_bytes(binary.read_bytes() + b"changed")

        with self.assertRaises(ValueError):
            self.run_observation(after_process=mutate)
        self.assertEqual(len(self.calls), 3)

    def test_all_reviewed_targets_pass_with_standard_harness(self):
        self.standard_harness_observation()

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
