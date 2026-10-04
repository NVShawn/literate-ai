"""Shared test fixtures extracted from test_retained_cargo_test_observation."""

import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.retained_cargo_test_observation import (
    select_cargo_test_executables,
    verify_libtest_observation,
)
from literate_ai.contracts.cargo_workspace import (
    CargoPackageExpectation,
    CargoTargetExpectation,
    CargoWorkspaceExpectation,
)


def process(stdout, code=0):
    return BoundedProcessResult(code, stdout.encode(), b"")


@unittest.skipUnless(
    shutil.which("cargo"), "Cargo is required for native test attribution"
)
class CargoTestExecutableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="ct-")
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name).resolve()
        files = {
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
        for name, text in files.items():
            path = cls.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        cls.environment = {
            **os.environ,
            "CARGO_HOME": str(cls.root / "home"),
            "CARGO_TARGET_DIR": str(cls.root / "out"),
            "CARGO_NET_OFFLINE": "true",
        }
        cls.run_cargo("generate-lockfile", "--offline")
        cls.metadata = json.loads(
            cls.run_cargo(
                "metadata", "--locked", "--offline", "--format-version=1"
            ).stdout
        )
        cls.compiled = cls.run_cargo(
            "test",
            "--workspace",
            "--all-targets",
            "--no-run",
            "--locked",
            "--offline",
            "--message-format=json",
        )
        cls.extra_metadata = json.loads(
            cls.run_cargo(
                "metadata",
                "--locked",
                "--offline",
                "--format-version=1",
                "--features=extra",
            ).stdout
        )
        cls.extra_compiled = cls.run_cargo(
            "test",
            "--workspace",
            "--all-targets",
            "--no-run",
            "--locked",
            "--offline",
            "--message-format=json",
            "--features=extra",
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

    def select(self, result=None, **changes):
        return select_cargo_test_executables(
            result or self.compiled,
            self.metadata,
            workspace_root=self.root,
            output_root=self.root / "out",
            expected=self.expected,
            **changes,
        )

    def test_all_targets_includes_false_flags_and_skips_inactive_features(self):
        binaries = self.select()
        self.assertEqual(
            {b.target_name for b in binaries},
            {
                "test_protocol",
                "integration",
                "custom",
                "no-tests",
                "empty",
                "example",
                "bench",
            },
        )
        self.assertEqual(len(binaries), 7)
        active = replace(
            self.expected,
            packages=(replace(self.expected.packages[0], features=("extra",)),),
        )
        binaries = select_cargo_test_executables(
            self.extra_compiled,
            self.extra_metadata,
            workspace_root=self.root,
            output_root=self.root / "out",
            expected=active,
        )
        self.assertEqual(
            {b.target_name for b in binaries},
            {
                "test_protocol",
                "integration",
                "custom",
                "no-tests",
                "empty",
                "example",
                "bench",
                "gated",
            },
        )

    def test_real_discovery_and_execution_are_attributed_per_binary(self):
        expected = {
            "test_protocol": ("duplicated_name", "panics"),
            "integration": ("duplicated_name",),
            "no-tests": ("bin_case",),
            "empty": (),
            "example": ("example_case",),
            "bench": ("bench_case",),
        }
        for binary in self.select():
            observations = []
            for args in (
                ("--list", "--format=terse"),
                ("--format=pretty", "--color=never"),
            ):
                result = subprocess.run(
                    [str(binary.executable), *args],
                    cwd=self.root,
                    env=self.environment,
                    capture_output=True,
                    timeout=60,
                )
                observations.append(
                    BoundedProcessResult(
                        result.returncode, result.stdout, result.stderr
                    )
                )
            with self.subTest(target=binary.target_name):
                if binary.target_name == "custom":
                    with self.assertRaisesRegex(ValueError, "discovery-mismatch"):
                        verify_libtest_observation(*observations, ())
                else:
                    self.assertEqual(
                        verify_libtest_observation(
                            *observations, expected[binary.target_name]
                        ),
                        expected[binary.target_name],
                    )

    def test_missing_duplicate_foreign_and_failed_artifact_records_refuse(self):
        original = [json.loads(line) for line in self.compiled.stdout.splitlines()]
        index = next(
            i
            for i, r in enumerate(original)
            if r["reason"] == "compiler-artifact" and r["profile"]["test"]
        )
        variants = []
        value = copy.deepcopy(original)
        value.pop(index)
        variants.append(value)
        value = copy.deepcopy(original)
        value.insert(index, value[index])
        variants.append(value)
        for change in (
            {"executable": str(self.root / "outside")},
            {"package_id": "foreign"},
            {"features": ["unreviewed"]},
        ):
            value = copy.deepcopy(original)
            value[index].update(change)
            variants.append(value)
        value = copy.deepcopy(original)
        value[-1]["success"] = False
        variants.extend((value, original[:-1]))
        for variant in variants:
            with self.subTest(variant=len(variant)), self.assertRaises(ValueError):
                self.select(process("\n".join(json.dumps(r) for r in variant)))
        with self.assertRaises(ValueError):
            self.select(maximum_records=1)
        with self.assertRaises(ValueError):
            self.select(maximum_bytes=1)
