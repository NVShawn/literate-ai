"""Synthetic reviewed producer bytes, real complete native v2 consumer processes.

The producer lifecycle is the existing contract fixture, never a claim that a
model/provider was exercised. Cargo, Rust, path patches and every receiving gate
are measured/executed natively in a fresh owned test workspace.
"""

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.builders.cargo import discover_cargo_toolchain
from literate_ai.adapters.builders.rust import discover_rust_toolchain
from literate_ai.adapters.directory_artifacts import DirectoryExportFile
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.contracts.cargo_workspace import (
    CargoPackageExpectation,
    CargoTargetExpectation,
    CargoWorkspaceExpectation,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.repositories import RepositoryBuildCommand
from literate_ai.contracts.retained_cargo import CargoManifestChange
from literate_ai.contracts.retained_cargo_patches import (
    CargoPatchedDependencyExpectation,
    CargoRegistryPatchExpectation,
    CargoWorkspacePatchConsumer,
)
from literate_ai.contracts.retained_cargo_test_protocol import (
    RetainedCargoCustomProtocol,
    RetainedCargoExcludedProtocol,
    RetainedCargoIgnoredCase,
    RetainedCargoLibtestProtocol,
)
from tests.support.fixtures_test_retained_cargo_import import (
    RetainedCargoImportTests,
    blob,
)


def _rustup_home_before_isolation():
    if "RUSTUP_HOME" in os.environ:
        return os.environ["RUSTUP_HOME"]
    return str(Path.home() / ".rustup")


class NativeReceivingFixture(RetainedCargoImportTests):
    """All declarations are built before the real reviewed/live receiver opens."""

    def __init__(self, base):
        super().__init__()
        self.base = base

    def setUp(self):
        def package_files(name, manifest):
            return (
                DirectoryExportFile(
                    "source/Cargo.toml",
                    manifest.replace(b"[lib]", b'edition = "2021"\n[lib]'),
                    0o644,
                ),
                DirectoryExportFile(
                    "source/src/lib.rs",
                    b"pub fn run() -> u32 { 7 }\n"
                    b"#[test] fn reviewed_case() { assert_eq!(run(), 7); }\n"
                    b'#[test] #[ignore = "owned fixture ignored contract"] '
                    b'fn deliberately_ignored() { panic!("must stay ignored"); }\n',
                    0o644,
                ),
            )

        super().setUp(package_files=package_files)
        project = self.base / "converted"
        implementation = json.loads(
            (project / ".literate/legacy-lift-shift.json").read_bytes()
        )["implementation_directory"]
        workspace = project / implementation
        rustup_home = _rustup_home_before_isolation()
        home = self.base / "native-cargo-home"
        home.mkdir()
        self.environment = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.base),
            "CARGO_HOME": str(home),
            "CARGO_TARGET_DIR": str(workspace / "cargo-output"),
            "CARGO_NET_OFFLINE": "true",
            "CARGO_INCREMENTAL": "0",
            "CARGO_BUILD_JOBS": "2",
            "RUSTUP_AUTO_INSTALL": "0",
            "RUSTUP_HOME": rustup_home,
        }
        cargo = discover_cargo_toolchain(self.environment)
        rust = discover_rust_toolchain(self.environment)
        self.cargo = LocalComponentToolBinding.from_observed_toolchain(cargo)
        self.rustc = LocalComponentToolBinding.from_observed_toolchain(rust)
        host = next(
            line.removeprefix("host: ")
            for line in rust.version.splitlines()
            if line.startswith("host: ")
        )
        library = sorted(self.plan.graph.packages, key=lambda p: p.root)[0]
        members = tuple(sorted((*self.plan.graph.members, "app")))
        root_manifest = (
            '[workspace]\nresolver="2"\nmembers='
            + json.dumps(members)
            + "\n[patch.crates-io]\n"
            + json.dumps(library.name)
            + "={path="
            + json.dumps(library.root)
            + "}\n[profile.test]\ndebug=0\nincremental=false\n"
        ).encode()
        app_manifest = (
            '[package]\nname="receiver-app"\nversion="1.0.0"\nedition="2021"\n'
            "[dependencies]\nretained_dep={package="
            + json.dumps(library.name)
            + ',version="=1.0.0"}\n'
            '[[test]]\nname="custom"\npath="custom.rs"\nharness=false\n'
            '[[bin]]\nname="excluded"\npath="excluded.rs"\n'
        ).encode()
        authored = {
            "Cargo.toml": root_manifest,
            "app/Cargo.toml": app_manifest,
            "app/src/lib.rs": (
                b"#[test] fn consumer_case() { assert_eq!(retained_dep::run(), 7); }\n"
            ),
            "app/custom.rs": (
                b"fn main() { assert_eq!(std::env::args().nth(1).as_deref(), "
                b'Some("--owned-contract")); println!("custom contract passed"); }\n'
            ),
            "app/excluded.rs": (
                b'fn main() { panic!("excluded fixture must not run"); }\n'
            ),
        }
        for path, content in authored.items():
            file = workspace / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(content)
        # Provision only the fixture's own canonical export bytes for prewarming.
        # It is retired again before the receiver captures any live authority.
        from literate_ai.adapters.directory_artifacts import read_directory_export

        export_paths = dict(self.binding.destinations)
        for manifest in self.products.exports.graph.manifests:
            for export in manifest.exports:
                destination = workspace / export_paths[export.identity]
                name = export.export_id.removeprefix("artifact-")
                payload = self.artifact_payloads[name]
                for item in read_directory_export(
                    payload,
                    blob(payload, "application/zip"),
                    max_bytes=4096,
                    max_entries=10,
                ):
                    file = destination / item.path
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_bytes(item.content)
                    file.chmod(item.mode)

        def command(*args):
            result = subprocess.run(
                [*cargo.command, *args],
                cwd=workspace,
                env=self.environment,
                capture_output=True,
                timeout=90,
            )
            if result.returncode:
                raise AssertionError(result.stderr.decode(errors="replace")[-4096:])
            return result

        command("generate-lockfile", "--offline")
        compile_result = command(
            "test",
            "--workspace",
            "--all-targets",
            "--no-run",
            "--locked",
            "--offline",
            "--target",
            host,
            "--target-dir",
            "cargo-output/prewarm",
            "--message-format=json",
        )
        custom = [
            Path(row["executable"])
            for line in compile_result.stdout.splitlines()
            if (row := json.loads(line)).get("reason") == "compiler-artifact"
            and row.get("target", {}).get("name") == "custom"
            and row.get("executable")
        ]
        assert len(custom) == 1
        custom_identity = ContentIdentity.parse_uri(
            "sha256:" + hashlib.sha256(custom[0].read_bytes()).hexdigest()
        )
        app = CargoPackageExpectation(
            "app",
            "receiver-app",
            "1.0.0",
            (),
            (
                CargoTargetExpectation(
                    "receiver_app",
                    ("lib",),
                    ("lib",),
                    "app/src/lib.rs",
                    "2021",
                    True,
                    True,
                ),
                CargoTargetExpectation(
                    "custom",
                    ("test",),
                    ("bin",),
                    "app/custom.rs",
                    "2021",
                    True,
                    False,
                ),
                CargoTargetExpectation(
                    "excluded",
                    ("bin",),
                    ("bin",),
                    "app/excluded.rs",
                    "2021",
                    True,
                    False,
                ),
            ),
        )
        graph = CargoWorkspaceExpectation(
            tuple(sorted((*self.plan.graph.packages, app), key=lambda p: p.root)),
            (),
            members,
            members,
            None,
            "cargo-output",
        )
        self.registry_patches = (
            CargoRegistryPatchExpectation(
                "crates-io",
                library.name,
                "registry+https://github.com/rust-lang/crates.io-index",
                library.name,
                "1.0.0",
                library.root,
                (
                    CargoPatchedDependencyExpectation(
                        CargoWorkspacePatchConsumer("app"),
                        "retained_dep",
                        "retained_dep",
                        None,
                        None,
                        (),
                        True,
                        False,
                        "=1.0.0",
                    ),
                ),
            ),
        )
        manifest_bytes = {
            "Cargo.toml": root_manifest,
            "Cargo.lock": (workspace / "Cargo.lock").read_bytes(),
            "app/Cargo.toml": app_manifest,
            **{
                package.root + "/Cargo.toml": self.native_manifests[package.name]
                for package in self.plan.graph.packages
            },
        }
        gates = (
            RepositoryBuildCommand(
                "full-native-workspace-check",
                (
                    "cargo",
                    "check",
                    "--workspace",
                    "--all-targets",
                    "--locked",
                    "--offline",
                ),
                implementation,
            ),
        )
        # Exact gates are prewarmed before capturing the complete Cargo home.
        command("check", "--workspace", "--all-targets", "--locked", "--offline")
        self.plan = replace(
            self.plan,
            graph=graph,
            manifests=tuple(
                CargoManifestChange(path, blob(b"before"), blob(content))
                for path, content in sorted(manifest_bytes.items())
            ),
            cargo_identity=self.cargo.toolchain_identity,
            rustc_identity=self.rustc.toolchain_identity,
            target=host,
            gates=gates,
        )
        self.inputs = replace(
            self.inputs,
            current_cargo_identity=self.plan.cargo_identity,
            current_rustc_identity=self.plan.rustc_identity,
            current_gates=gates,
        )
        libtest = RetainedCargoLibtestProtocol(
            ("reviewed_case",),
            (
                RetainedCargoIgnoredCase(
                    "deliberately_ignored", "owned fixture ignored contract"
                ),
            ),
        )
        self.permission_bytes = b"independent owned native fixture exclusion review\n"
        self.permission_path = self.base / "exclusion-permission"
        self.permission_path.write_bytes(self.permission_bytes)
        self.permission_identity = canonical_identity(self.permission_bytes.decode())
        self.protocols = {
            (p.root, t.name, tuple(sorted(t.kinds))): libtest
            for p in self.plan.graph.packages
            if p.root != "app"
            for t in p.targets
        }
        self.protocols.update(
            {
                ("app", "receiver_app", ("lib",)): RetainedCargoLibtestProtocol(
                    ("consumer_case",), ()
                ),
                ("app", "custom", ("test",)): RetainedCargoCustomProtocol(
                    RepositoryBuildCommand(
                        "custom-contract",
                        ("{target_executable}", "--owned-contract"),
                        implementation + "/app",
                    ),
                    custom_identity,
                    (),
                    15,
                    4096,
                ),
                ("app", "excluded", ("bin",)): RetainedCargoExcludedProtocol(
                    "separately reviewed owned-fixture exclusion",
                    self.permission_identity,
                ),
            }
        )
        self.harnesses = {("app", "custom", ("test",)): False}
        for destination in export_paths.values():
            shutil.rmtree(workspace / destination)
        (workspace / "pkgs").rmdir()
        shutil.rmtree(workspace / "cargo-output")

    def read_permission(self, identity):
        assert identity == self.permission_identity
        return canonical_identity(self.permission_path.read_bytes().decode())
