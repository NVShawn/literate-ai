"""Retained graph checks against real, model-free, offline Cargo resolution."""

from __future__ import annotations

import copy
import hashlib
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
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.repositories import RepositoryBuildCommand
from literate_ai.contracts.retained_cargo import (
    CargoManifestChange,
    RetainedCargoWorkspacePlan,
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

    def test_reviewed_graph_roundtrip_still_matches_real_cargo(self) -> None:
        reopened = CargoWorkspaceExpectation.from_dict(self.expected.to_dict())
        self.assertEqual(reopened, self.expected)
        verify_cargo_workspace_graph(
            self.metadata, workspace_root=self.root, expected=reopened
        )

    def test_plan_metadata_command_observes_the_reviewed_native_graph(self) -> None:
        manifests = []
        for name, content in sorted(self.before.items()):
            if Path(name).name in {"Cargo.toml", "Cargo.lock"}:
                ref = BlobRef(hashlib.sha256(content).hexdigest(), len(content))
                manifests.append(CargoManifestChange(name, ref, ref))
        plan = RetainedCargoWorkspacePlan(
            ".",
            self.expected,
            tuple(manifests),
            canonical_identity("cargo-fixture"),
            canonical_identity("rustc-fixture"),
            "x86_64-unknown-linux-gnu",
            (),
            False,
            False,
            (
                RepositoryBuildCommand(
                    "existing-tests",
                    ("cargo", "test", "--workspace", "--all-targets", "--locked"),
                ),
            ),
        )
        command = plan.metadata_command(offline=True)
        before = self.inventory()
        result = subprocess.run(
            command.argv,
            cwd=self.root / command.working_directory,
            env={**self.environment, **{e.name: e.value for e in command.environment}},
            capture_output=True,
            check=True,
            timeout=60,
        )
        self.verify(json.loads(result.stdout), expected=plan.graph)
        self.assertEqual(before, self.inventory())
        self.assertFalse(command.network)

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

    def test_dependency_alias_kind_target_and_feature_drift_refuse(self) -> None:
        for field, value in (
            ("rename", "wrong"),
            ("kind", "dev"),
            ("target", "cfg(windows)"),
            ("features", []),
            ("uses_default_features", True),
            ("optional", True),
            ("req", "^99"),
        ):
            with self.subTest(field=field):
                data = copy.deepcopy(self.metadata)
                client = next(p for p in data["packages"] if p["name"] == "client")
                dep = next(d for d in client["dependencies"] if d["kind"] is None)
                dep[field] = value
                with self.assertRaises(CargoWorkspaceGraphError):
                    self.verify(data)

    def test_resolved_alias_feature_and_missing_edge_refuse(self) -> None:
        for change in ("name", "features", "edge"):
            with self.subTest(change=change):
                data = copy.deepcopy(self.metadata)
                client_id = next(
                    p["id"] for p in data["packages"] if p["name"] == "client"
                )
                client = next(
                    n for n in data["resolve"]["nodes"] if n["id"] == client_id
                )
                if change == "name":
                    client["deps"][0]["name"] = "unreviewed"
                elif change == "features":
                    client["features"] = ["unreviewed"]
                else:
                    client["deps"][0]["dep_kinds"].pop()
                with self.assertRaises(CargoWorkspaceGraphError):
                    self.verify(data)

    def test_membership_and_test_target_loss_refuse(self) -> None:
        for field in (
            "workspace_members",
            "workspace_default_members",
            "targets",
            "test",
        ):
            with self.subTest(field=field):
                data = copy.deepcopy(self.metadata)
                if field.startswith("workspace"):
                    data[field].pop()
                else:
                    client = next(p for p in data["packages"] if p["name"] == "client")
                    if field == "targets":
                        client[field].pop()
                    else:
                        next(t for t in client["targets"] if t["kind"] == ["test"])[
                            "test"
                        ] = False
                with self.assertRaises(CargoWorkspaceGraphError):
                    self.verify(data)

    def test_malformed_incomplete_and_duplicate_metadata_refuse_without_leaking(
        self,
    ) -> None:
        cases = [
            None,
            [],
            {},
            {**self.metadata, "version": True},
            {**self.metadata, "resolve": None},
        ]
        duplicate = copy.deepcopy(self.metadata)
        duplicate["packages"].append(duplicate["packages"][0])
        cases.append(duplicate)
        for data in cases:
            with self.subTest(data_type=type(data).__name__):
                with self.assertRaises(CargoWorkspaceGraphError) as error:
                    verify_cargo_workspace_graph(
                        data, workspace_root=self.root, expected=self.expected
                    )
                self.assertNotIn(str(self.root), str(error.exception))
                self.assertTrue(str(error.exception).startswith("cargo.workspace."))

    def test_reviewed_expectation_is_required_and_duplicate_edges_refuse(self) -> None:
        with self.assertRaises(CargoWorkspaceGraphError):
            self.verify(
                expected=replace(
                    self.expected, dependencies=self.expected.dependencies[:-1]
                )
            )
        with self.assertRaises(CargoWorkspaceGraphError):
            replace(
                self.expected,
                dependencies=self.expected.dependencies
                + self.expected.dependencies[:1],
            )
        with self.assertRaises(CargoWorkspaceGraphError):
            replace(self.expected.packages[0], root="../foreign")
        with self.assertRaises(CargoWorkspaceGraphError):
            replace(self.expected, output_directory="pkgs/provider/target")

    def test_inactive_optional_edge_and_output_substitution_refuse(self) -> None:
        self.assertTrue(any(not d.resolved for d in self.expected.dependencies))
        for field in ("optional", "target_directory", "build_directory"):
            data = copy.deepcopy(self.metadata)
            if field == "optional":
                client = next(p for p in data["packages"] if p["name"] == "client")
                client["dependencies"] = [
                    d for d in client["dependencies"] if d["rename"] != "opt"
                ]
            else:
                data[field] = str(self.root / "pkgs/provider/target")
            with self.assertRaises(CargoWorkspaceGraphError):
                self.verify(data)

    def test_cargo_preserves_conditional_kinds_on_an_already_resolved_package(
        self,
    ) -> None:
        # --filter-platform does not erase every nonmatching dep_kind when another
        # declaration already resolves the same package. Do not treat kinds as cfg
        # execution evidence, and do not silently discard them from comparison.
        client_id = next(
            p["id"] for p in self.metadata["packages"] if p["name"] == "client"
        )
        client = next(
            n for n in self.metadata["resolve"]["nodes"] if n["id"] == client_id
        )
        self.assertIn(
            {"kind": None, "target": "cfg(windows)"}, client["deps"][0]["dep_kinds"]
        )
        self.verify()

    def test_package_ids_are_opaque_and_additive_metadata_is_compatible(self) -> None:
        data = copy.deepcopy(self.metadata)
        ids = {p["id"]: f"opaque-package-{i}" for i, p in enumerate(data["packages"])}
        for package in data["packages"]:
            package["id"] = ids[package["id"]]
            package["future_cargo_field"] = {"arbitrary": [1, 2, 3]}
        for field in ("workspace_members", "workspace_default_members"):
            data[field] = [ids[identity] for identity in data[field]]
        for node in data["resolve"]["nodes"]:
            node["id"] = ids[node["id"]]
            node["dependencies"] = [ids[identity] for identity in node["dependencies"]]
            for dep in node["deps"]:
                dep["pkg"] = ids[dep["pkg"]]
        self.verify(data)


if __name__ == "__main__":
    unittest.main()
