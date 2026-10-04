"""Retained graph checks against real, model-free, offline Cargo resolution."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.cargo_workspace_graph import (
    CargoDependencyExpectation,
    CargoPackageExpectation,
    CargoTargetExpectation,
    CargoWorkspaceExpectation,
    CargoWorkspaceGraphError,
    verify_cargo_workspace_graph,
)


@unittest.skipUnless(
    shutil.which("cargo"), "Cargo is required for native graph fixtures"
)
class CargoWorkspaceGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="cg-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        files = {
            "Cargo.toml": """[workspace]
resolver = "2"
members = ["client", "pkgs/*"]
default-members = ["client"]
""",
            "client/Cargo.toml": """[package]
name = "client"
version = "0.1.0"
edition = "2021"
[dependencies.api]
package = "provider"
path = "../pkgs/provider"
features = ["extra"]
default-features = false
[dependencies.opt]
package = "helper"
path = "../pkgs/helper"
optional = true
[dev-dependencies.api]
package = "provider"
path = "../pkgs/provider"
features = ["extra"]
default-features = false
[build-dependencies.api]
package = "provider"
path = "../pkgs/provider"
features = ["extra"]
default-features = false
[target.'cfg(windows)'.dependencies.api]
package = "provider"
path = "../pkgs/provider"
features = ["extra"]
default-features = false
""",
            "client/src/main.rs": "fn main() {}\n",
            "client/build.rs": "fn main() {}\n",
            "client/tests/smoke.rs": "#[test] fn smoke() {}\n",
            "pkgs/provider/Cargo.toml": """[package]
name = "provider"
version = "1.2.3"
edition = "2021"
[features]
extra = []
[dependencies]
helper = { path = "../helper" }
""",
            "pkgs/provider/src/lib.rs": "pub fn value() -> u32 { 42 }\n",
            "pkgs/helper/Cargo.toml": """[package]
name = "helper"
version = "0.2.0"
edition = "2021"
""",
            "pkgs/helper/src/lib.rs": "pub fn help() {}\n",
        }
        for name, text in files.items():
            path = cls.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        # Give Cargo a fresh, explicit home and no external dependency to retrieve.
        cls.environment = {
            **os.environ,
            "CARGO_HOME": str(cls.root / "cargo-home"),
            "CARGO_TARGET_DIR": str(cls.root / "cargo-output"),
            "CARGO_NET_OFFLINE": "true",
        }
        cls.run_cargo("generate-lockfile", "--offline")
        cls.before = cls.inventory()
        cls.metadata = json.loads(
            cls.run_cargo(
                "metadata",
                "--locked",
                "--offline",
                "--format-version=1",
                "--filter-platform",
                "x86_64-unknown-linux-gnu",
            )
        )
        cls.after = cls.inventory()
        target = CargoTargetExpectation
        cls.expected = CargoWorkspaceExpectation(
            packages=(
                CargoPackageExpectation(
                    "client",
                    "client",
                    "0.1.0",
                    (),
                    (
                        target(
                            "client",
                            ("bin",),
                            ("bin",),
                            "client/src/main.rs",
                            "2021",
                            True,
                            False,
                        ),
                        target(
                            "build-script-build",
                            ("custom-build",),
                            ("bin",),
                            "client/build.rs",
                            "2021",
                            False,
                            False,
                        ),
                        target(
                            "smoke",
                            ("test",),
                            ("bin",),
                            "client/tests/smoke.rs",
                            "2021",
                            True,
                            False,
                        ),
                    ),
                ),
                CargoPackageExpectation(
                    "pkgs/provider",
                    "provider",
                    "1.2.3",
                    ("extra",),
                    (
                        target(
                            "provider",
                            ("lib",),
                            ("lib",),
                            "pkgs/provider/src/lib.rs",
                            "2021",
                            True,
                            True,
                        ),
                    ),
                ),
                CargoPackageExpectation(
                    "pkgs/helper",
                    "helper",
                    "0.2.0",
                    (),
                    (
                        target(
                            "helper",
                            ("lib",),
                            ("lib",),
                            "pkgs/helper/src/lib.rs",
                            "2021",
                            True,
                            True,
                        ),
                    ),
                ),
            ),
            dependencies=tuple(
                CargoDependencyExpectation(
                    "client",
                    "pkgs/provider",
                    "api",
                    "api",
                    kind,
                    None,
                    ("extra",),
                    False,
                    False,
                    "*",
                    True,
                )
                for kind in (None, "dev", "build")
            )
            + (
                CargoDependencyExpectation(
                    "pkgs/provider",
                    "pkgs/helper",
                    "helper",
                    "helper",
                    None,
                    None,
                    (),
                    True,
                    False,
                    "*",
                    True,
                ),
            ),
            members=("client", "pkgs/provider", "pkgs/helper"),
            default_members=("client",),
            root_package=None,
            output_directory="cargo-output",
        )
        cls.expected = replace(
            cls.expected,
            dependencies=cls.expected.dependencies
            + (
                CargoDependencyExpectation(
                    "client",
                    "pkgs/helper",
                    "opt",
                    "opt",
                    None,
                    None,
                    (),
                    True,
                    True,
                    "*",
                    False,
                ),
                CargoDependencyExpectation(
                    "client",
                    "pkgs/provider",
                    "api",
                    "api",
                    None,
                    "cfg(windows)",
                    ("extra",),
                    False,
                    False,
                    "*",
                    True,
                ),
            ),
        )

    @classmethod
    def run_cargo(cls, *arguments: str) -> bytes:
        result = subprocess.run(
            (shutil.which("cargo"), *arguments),
            cwd=cls.root,
            env=cls.environment,
            capture_output=True,
            timeout=60,
            check=True,
        )
        return result.stdout

    @classmethod
    def inventory(cls) -> dict[str, bytes]:
        return {
            p.relative_to(cls.root).as_posix(): p.read_bytes()
            for p in cls.root.rglob("*")
            if p.is_file() and "cargo-home" not in p.parts
        }

    def verify(self, metadata=None, expected=None) -> None:
        verify_cargo_workspace_graph(
            self.metadata if metadata is None else metadata,
            workspace_root=self.root,
            expected=self.expected if expected is None else expected,
        )

    def test_real_locked_offline_graph_preserves_every_target_and_manifest(
        self,
    ) -> None:
        self.verify()
        self.assertEqual(self.before, self.after)
        self.assertEqual(self.after, self.inventory())
        self.assertFalse((self.root / "cargo-output").exists())

    def test_package_root_name_version_and_registry_substitution_refuse(self) -> None:
        for field, value in (
            ("manifest_path", str(self.root / "old/Cargo.toml")),
            ("name", "substitute"),
            ("version", "9.0.0"),
            ("source", "registry+https://example.invalid/"),
        ):
            with self.subTest(field=field):
                data = copy.deepcopy(self.metadata)
                provider = next(p for p in data["packages"] if p["name"] == "provider")
                provider[field] = value
                with self.assertRaises(CargoWorkspaceGraphError):
                    self.verify(data)


if __name__ == "__main__":
    unittest.main()
