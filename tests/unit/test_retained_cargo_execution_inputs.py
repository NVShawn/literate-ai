"""Cargo source/config custody, including real provisioned dependency resolution."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.retained_cargo_execution_inputs import (
    read_retained_cargo_execution_inputs,
)
from tests.support import fixtures_test_retained_cargo_materialization as fixtures


class RetainedCargoExecutionInputsTests(unittest.TestCase):
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

    def test_config_absence_and_later_registry_addition_invalidate(self):
        for path in (self.home / "config.toml", self.home / "registry"):
            with self.subTest(path=path.name):
                snapshot = self.capture()
                if path.name == "registry":
                    path.mkdir()
                else:
                    path.write_text("[net]\noffline = true\n")
                with self.assertRaises(ValueError):
                    snapshot.current_identity()
                self.capture().current_identity()

    def test_external_ancestor_configuration_is_pinned_including_absence(self):
        parent = self.scratch / "parent"
        parent.mkdir()
        root = parent / "consumer"
        shutil.copytree(self.root, root)
        self.fixture.root = root
        self.materialized = self.fixture.run_materializer()
        configuration = parent / ".cargo"
        configuration.mkdir()
        old = configuration / "config"
        old.write_text("[net]\noffline=true\n")
        snapshot = self.capture()
        old.write_text("[net]\noffline=false\n")
        with self.assertRaises(ValueError):
            snapshot.current_identity()
        snapshot = self.capture()
        (configuration / "config.toml").write_text("[net]\noffline=true\n")
        with self.assertRaises(ValueError):
            snapshot.current_identity()

    def test_dependency_edits_and_identical_replacements_refuse(self):
        root = self.home / "registry/src/example"
        root.mkdir(parents=True)
        source = root / "lib.rs"
        source.write_text("pub fn value() -> u8 { 1 }\n")
        for replacement in (b"changed", source.read_bytes()):
            snapshot = self.capture()
            before = snapshot.current_identity()
            source.unlink()
            source.write_bytes(replacement)
            with self.assertRaises(ValueError):
                snapshot.current_identity()
            current = self.capture()
            if replacement == b"changed":
                self.assertNotEqual(current.current_identity(), before)

    def test_cache_tag_timestamp_rewrites_preserve_exact_input_identity(self):
        for name in ("git", "registry"):
            with self.subTest(cache=name):
                root = self.home / name
                root.mkdir()
                tag = root / "CACHEDIR.TAG"
                content = b"Signature: 8a477f597d28d172789f06886806bc55\n"
                tag.write_bytes(content)
                snapshot = self.capture()
                before = snapshot.current_identity()
                node = tag.stat()
                tag.write_bytes(content)
                os.utime(tag, ns=(node.st_atime_ns, node.st_mtime_ns + 1000000000))
                self.assertEqual(snapshot.current_identity(), before)

    def test_cache_tag_changed_bytes_and_replacement_still_refuse(self):
        root = self.home / "git"
        root.mkdir()
        tag = root / "CACHEDIR.TAG"
        tag.write_bytes(b"original marker")
        snapshot = self.capture()
        tag.write_bytes(b"modified marker")
        with self.assertRaises(ValueError):
            snapshot.current_identity()
        snapshot = self.capture()
        replacement = self.scratch / "replacement"
        replacement.write_bytes(tag.read_bytes())
        replacement.replace(tag)
        with self.assertRaises(ValueError):
            snapshot.current_identity()

    def test_nested_cache_tag_timestamp_changes_still_refuse(self):
        root = self.home / "git/checkouts/example"
        root.mkdir(parents=True)
        tag = root / "CACHEDIR.TAG"
        tag.write_bytes(b"source-owned marker")
        snapshot = self.capture()
        node = tag.stat()
        os.utime(tag, ns=(node.st_atime_ns, node.st_mtime_ns + 1000000000))
        with self.assertRaises(ValueError):
            snapshot.current_identity()

    def test_metadata_cannot_name_uncaptured_manifest_or_target(self):
        snapshot = self.capture()
        for package in (
            {"manifest_path": str(self.scratch / "outside/Cargo.toml"), "targets": []},
            {
                "manifest_path": str(self.root / "Cargo.toml"),
                "targets": [{"src_path": str(self.scratch / "outside.rs")}],
            },
        ):
            with (
                self.subTest(package=package),
                self.assertRaisesRegex(ValueError, "uncaptured-package-path"),
            ):
                snapshot.require_metadata_paths({"packages": [package]})

    def test_finite_bounds_and_dependency_links_refuse(self):
        root = self.home / "git"
        root.mkdir()
        (root / "first").write_bytes(b"1234")
        with self.assertRaises(ValueError):
            self.capture(maximum_file_bytes=1, maximum_total_bytes=1)
        try:
            (root / "second").symlink_to(root / "first")
        except OSError:
            self.skipTest("host does not permit symbolic links")
        with self.assertRaises(ValueError):
            self.capture()

    def test_git_hardlinks_require_every_alias_inside_captured_tree(self):
        root = self.home / "git"
        root.mkdir()
        first, second = root / "pack", root / "checkout-pack"
        first.write_bytes(b"pack bytes")
        try:
            os.link(first, second)
        except OSError:
            self.skipTest("host does not support hard links")
        snapshot = self.capture()
        snapshot.current_identity()
        outside = self.scratch / "foreign-alias"
        os.link(first, outside)
        with self.assertRaisesRegex(ValueError, "external-hardlink"):
            snapshot.current_identity()
        with self.assertRaisesRegex(ValueError, "external-hardlink"):
            self.capture()
        outside.unlink()
        snapshot = self.capture()
        second.write_bytes(b"changed via captured alias")
        with self.assertRaises(ValueError):
            snapshot.current_identity()

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
