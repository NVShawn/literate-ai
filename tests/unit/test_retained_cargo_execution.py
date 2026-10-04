"""Native execution sequencing with process doubles and one real offline build."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
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


class RetainedCargoOfflineVendorBuildTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.RetainedCargoMaterializationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.materialized = fixture.run_materializer()
        self.root = fixture.root
        temporary = tempfile.TemporaryDirectory(prefix="ce-")
        self.addCleanup(temporary.cleanup)
        self.scratch = Path(temporary.name).resolve()
        self.home = self.scratch / "cargo-home"
        self.home.mkdir()
        self.environment = {
            "CARGO_HOME": str(self.home),
            "OBJ_DIR": "pkgs",
            "BUILD_DIR": "build",
        }

    def capture(self, **changes):
        return read_retained_cargo_execution_inputs(
            self.materialized, environment=self.environment, **changes
        )

    def test_real_vendor_and_git_sources_stay_current_through_offline_build(self):
        cargo, git = shutil.which("cargo"), shutil.which("git")
        if not cargo or not git:
            self.skipTest("Cargo and Git are required for native dependency custody")
        project = self.root / "native"
        project.mkdir()
        vendor = project / "vendor/helper"
        vendor.mkdir(parents=True)
        (vendor / "Cargo.toml").write_text(
            '[package]\nname="helper"\nversion="0.1.0"\nedition="2021"\n[lib]\npath="lib.rs"\n'
        )
        (vendor / "lib.rs").write_text("pub fn value() -> u8 { 3 }\n")
        checksum = {
            "package": "1" * 64,
            "files": {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in vendor.iterdir()
            },
        }
        (vendor / ".cargo-checksum.json").write_text(json.dumps(checksum))
        remote = self.scratch / "git-source"
        remote.mkdir()
        (remote / "Cargo.toml").write_text(
            '[package]\nname="git-helper"\nversion="0.1.0"\nedition="2021"\n[lib]\npath="lib.rs"\n'
        )
        (remote / "lib.rs").write_text("pub fn value() -> u8 { 4 }\n")

        def command(argv, cwd, environment=None):
            result = subprocess.run(
                argv, cwd=cwd, env=environment, capture_output=True, timeout=120
            )
            self.assertEqual(
                result.returncode, 0, result.stderr.decode(errors="replace")
            )
            return result.stdout

        command([git, "init", "--quiet"], remote)
        command([git, "add", "."], remote)
        command(
            [
                git,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--quiet",
                "-m",
                "fixture",
            ],
            remote,
        )
        (project / "Cargo.toml").write_text(
            '[workspace]\n[package]\nname="consumer"\nversion="0.1.0"\nedition="2021"\n[lib]\npath="lib.rs"\n[dependencies]\nhelper="=0.1.0"\ngit-helper={git='
            + json.dumps(remote.as_uri())
            + "}\n"
        )
        (project / "lib.rs").write_text(
            "pub fn value() -> u8 { helper::value() + git_helper::value() }\n"
        )
        (project / ".cargo").mkdir()
        (project / ".cargo/config.toml").write_text(
            '[source.crates-io]\nreplace-with="fixture"\n[source.fixture]\ndirectory="vendor"\n'
        )
        self.environment = {
            **os.environ,
            **self.environment,
            "CARGO_TARGET_DIR": str(self.root / "cargo-output"),
        }
        # Provision the explicitly local Git source before capture. Registry
        # resolution uses only the authored vendor directory.
        command(
            [cargo, "generate-lockfile"],
            project,
            {**self.environment, "CARGO_NET_OFFLINE": "false"},
        )
        self.environment["CARGO_NET_OFFLINE"] = "true"
        snapshot = self.capture()
        before = snapshot.current_identity()
        metadata = json.loads(
            command(
                [cargo, "metadata", "--format-version", "1", "--locked", "--offline"],
                project,
                self.environment,
            )
        )
        snapshot.require_metadata_paths(metadata)
        sources = [p["source"] for p in metadata["packages"] if p["source"]]
        self.assertTrue(any(source.startswith("registry+") for source in sources))
        self.assertTrue(any(source.startswith("git+") for source in sources))
        command([cargo, "build", "--locked", "--offline"], project, self.environment)
        self.assertEqual(snapshot.current_identity(), before)
        git_package = next(
            p for p in metadata["packages"] if (p["source"] or "").startswith("git+")
        )
        Path(git_package["targets"][0]["src_path"]).write_text(
            "pub fn value() -> u8 { 9 }\n"
        )
        with self.assertRaises(ValueError):
            snapshot.current_identity()
