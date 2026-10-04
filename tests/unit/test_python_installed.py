from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from packaging.markers import default_environment

from literate_ai.adapters.dependencies.python_installed import (
    observe_python_installation,
)
from literate_ai.adapters.dependencies.python_lock import parse_python_wheel_lock
from literate_ai.adapters.dependencies.python_wheelhouse import stage_python_wheels
from literate_ai.adapters.dependencies.types import DependencyObservationError
from tests.support.fixtures_test_python_wheel_lock import document, wheel_record


class PythonInstalledTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.installed = self.root / "installed"
        self.installed.mkdir()
        self.value = document()
        self.sources = {
            "example": wheel_record(dependencies=("helper>=1",))[1],
            "helper": wheel_record("helper")[1],
        }
        self.scheme = {
            "purelib": ".",
            "platlib": ".",
            "scripts": "bin",
            "headers": "include",
            "data": "data",
        }

    def stage(self):
        return stage_python_wheels(
            parse_python_wheel_lock(json.dumps(self.value)),
            {name: io.BytesIO(content) for name, content in self.sources.items()},
            environment=default_environment(),
            tags=("py3-none-any",),
            temporary_root=self.root,
        )

    def record(self, name, paths, algorithm="sha256"):
        base = self.installed / self.scheme["purelib"]
        record_path = base / (name + "-1.0.dist-info/RECORD")
        content = io.StringIO(newline="")
        writer = csv.writer(content)
        for path in sorted(paths):
            data = path.read_bytes()
            digest = (
                base64.urlsafe_b64encode(hashlib.new(algorithm, data).digest())
                .rstrip(b"=")
                .decode()
            )
            writer.writerow(
                [
                    os.path.relpath(path, base).replace(os.sep, "/"),
                    algorithm + "=" + digest,
                    len(data),
                ]
            )
        writer.writerow([name + "-1.0.dist-info/RECORD", "", ""])
        record_path.write_text(content.getvalue(), encoding="utf-8", newline="")

    def install_fixture(self):
        # Independent fixture materialization, not the production observer's mapper.
        self.owned = {}
        for name, content in self.sources.items():
            paths = []
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                for member in archive.namelist():
                    if member.endswith("/RECORD"):
                        continue
                    if ".data/" in member:
                        _, relative = member.split(".data/", 1)
                        key, relative = relative.split("/", 1)
                        path = self.installed / self.scheme[key] / relative
                    else:
                        path = self.installed / self.scheme["purelib"] / member
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(archive.read(member))
                    paths.append(path)
            base = self.installed / self.scheme["purelib"] / (name + "-1.0.dist-info")
            for filename, data in (("INSTALLER", b"pip\n"), ("REQUESTED", b"")):
                path = base / filename
                path.write_bytes(data)
                paths.append(path)
            self.owned[name] = paths
            self.record(name, paths)

    def replace_wheel_files(self, name, changes):
        with zipfile.ZipFile(io.BytesIO(self.sources[name])) as archive:
            files = {path: archive.read(path) for path in archive.namelist()}
        files.update(changes)
        record_path = name + "-1.0.dist-info/RECORD"
        content = io.StringIO(newline="")
        writer = csv.writer(content)
        for path, data in sorted(files.items()):
            if path != record_path:
                digest = (
                    base64.urlsafe_b64encode(hashlib.sha256(data).digest())
                    .rstrip(b"=")
                    .decode()
                )
                writer.writerow([path, "sha256=" + digest, len(data)])
        writer.writerow([record_path, "", ""])
        files[record_path] = content.getvalue().encode()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for path, data in sorted(files.items()):
                archive.writestr(zipfile.ZipInfo(path), data)
        self.sources[name] = stream.getvalue()
        next(record for record in self.value["packages"] if record["name"] == name)[
            "sha256"
        ] = hashlib.sha256(stream.getvalue()).hexdigest()

    def observe(self, staged):
        return observe_python_installation(staged, self.installed, scheme=self.scheme)

    def test_enumeration_metadata_does_not_supply_custody_identity(self):
        self.install_fixture()
        real_scandir = os.scandir

        @contextmanager
        def incomplete_scandir(directory):
            with real_scandir(directory) as entries:
                rows = []
                for entry in entries:
                    observed = Path(entry.path).lstat()
                    incomplete = SimpleNamespace(
                        st_mode=observed.st_mode,
                        st_file_attributes=getattr(observed, "st_file_attributes", 0),
                        st_dev=0,
                        st_ino=0,
                        st_nlink=0,
                    )
                    rows.append(
                        SimpleNamespace(
                            path=entry.path,
                            stat=lambda *, follow_symlinks=False, value=incomplete: (
                                value
                            ),
                        )
                    )
                yield iter(rows)

        with self.stage() as staged:
            with patch("os.scandir", incomplete_scandir):
                result = self.observe(staged)
            self.assertTrue(result.files)

    def test_full_metadata_still_rejects_hardlinked_payload(self):
        self.install_fixture()
        payload = self.installed / "example/__init__.py"
        os.link(payload, self.root / "external-hardlink.py")
        with (
            self.stage() as staged,
            self.assertRaisesRegex(DependencyObservationError, "link or special"),
        ):
            self.observe(staged)

    def test_complete_graph_and_aliases_without_importing_code(self):
        self.replace_wheel_files(
            "example",
            {
                "example/__init__.py": b'raise AssertionError("must never import")\n',
                "example/native.pyd": b"MZfixture",
            },
        )
        self.install_fixture()
        with self.stage() as staged:
            result = self.observe(staged)
            self.assertEqual(
                result.graph.edges,
                (
                    ("@root", "pkg:pypi/example"),
                    ("pkg:pypi/example", "pkg:pypi/helper"),
                ),
            )
            self.assertEqual(len(result.graph.components), 2)
            self.assertIn(
                {"name": "literate-ai:python-top-level-import", "value": "example"},
                result.graph.components[0]["properties"],
            )
            self.assertEqual(result, self.observe(staged))

    def test_windows_like_layout_and_data_schemes(self):
        self.scheme = {
            "purelib": "Lib/site-packages",
            "platlib": "Lib/site-packages",
            "scripts": "Scripts",
            "headers": "Include",
            "data": "share",
        }
        self.replace_wheel_files(
            "example",
            {
                "example-1.0.data/data/resource.txt": b"data",
                "example-1.0.data/scripts/native.exe": b"MZfixture",
            },
        )
        self.install_fixture()
        with self.stage() as staged:
            result = self.observe(staged)
        self.assertIn("Scripts/native.exe", {item.path for item in result.files})
        self.assertIn("share/resource.txt", {item.path for item in result.files})

    def test_payload_change_cannot_be_hidden_by_rewriting_record(self):
        self.install_fixture()
        (self.installed / "example/__init__.py").write_bytes(b"modified = True\n")
        self.record("example", self.owned["example"])
        with (
            self.stage() as staged,
            self.assertRaisesRegex(DependencyObservationError, "verified wheel"),
        ):
            self.observe(staged)

    def test_extra_unrecorded_package_file(self):
        self.install_fixture()
        (self.installed / "undeclared.py").write_bytes(b"pass\n")
        with (
            self.stage() as staged,
            self.assertRaisesRegex(
                DependencyObservationError, "outside the locked closure"
            ),
        ):
            self.observe(staged)

    def test_missing_wheel_file(self):
        self.install_fixture()
        (self.installed / "helper/__init__.py").unlink()
        with self.stage() as staged, self.assertRaises(DependencyObservationError):
            self.observe(staged)

    def test_extra_recorded_file_not_in_wheel(self):
        self.install_fixture()
        extra = self.installed / "example/undeclared.py"
        extra.write_bytes(b"pass\n")
        self.record("example", [*self.owned["example"], extra])
        with self.stage() as staged, self.assertRaises(DependencyObservationError):
            self.observe(staged)

    def test_record_traversal_duplicate_and_hash_tampering(self):
        self.install_fixture()
        path = self.installed / "example-1.0.dist-info/RECORD"
        original = path.read_bytes()
        for content in (
            b"../../outside,sha256=no,0\n",
            original + original.splitlines(keepends=True)[0],
            original.replace(b"sha256=", b"sha512="),
            b"/absolute,,\n",
        ):
            with self.subTest(content=content):
                path.write_bytes(content)
                with (
                    self.stage() as staged,
                    self.assertRaises(DependencyObservationError),
                ):
                    self.observe(staged)

    def test_install_scheme_cannot_escape_root(self):
        self.install_fixture()
        self.scheme["scripts"] = "../outside"
        with self.stage() as staged, self.assertRaises(DependencyObservationError):
            self.observe(staged)

    def test_wheel_scheme_collision(self):
        self.replace_wheel_files(
            "example", {"example-1.0.data/purelib/example/__init__.py": b"VALUE = 1\n"}
        )
        self.install_fixture()
        with (
            self.stage() as staged,
            self.assertRaisesRegex(DependencyObservationError, "collide"),
        ):
            self.observe(staged)

    def test_identical_shared_payload_is_verified_for_each_owner(self):
        for name in self.sources:
            self.replace_wheel_files(name, {"shared/__init__.py": b"VALUE = 1\n"})
        self.install_fixture()
        with self.stage() as staged:
            result = self.observe(staged)
            self.assertEqual(result, self.observe(staged))
            self.assertEqual(len(result.graph.components), 2)
            for component in result.graph.components:
                self.assertIn(
                    {"name": "literate-ai:python-top-level-import", "value": "shared"},
                    component["properties"],
                )
            self.assertEqual(
                sum(file.path == "shared/__init__.py" for file in result.files), 1
            )

    def test_shared_payload_conflicting_wheel_bytes_fail(self):
        self.replace_wheel_files("example", {"shared/__init__.py": b"first\n"})
        self.replace_wheel_files("helper", {"shared/__init__.py": b"other\n"})
        self.install_fixture()
        with self.stage() as staged, self.assertRaises(DependencyObservationError):
            self.observe(staged)

    def test_shared_payload_each_record_must_cover_claim(self):
        for name in self.sources:
            self.replace_wheel_files(name, {"shared/__init__.py": b"VALUE = 1\n"})
        self.install_fixture()
        shared = self.installed / "shared/__init__.py"
        self.record("helper", [p for p in self.owned["helper"] if p != shared])
        with (
            self.stage() as staged,
            self.assertRaisesRegex(
                DependencyObservationError, "complete package payload"
            ),
        ):
            self.observe(staged)

    def test_shared_payload_tamper_cannot_be_hidden_by_both_records(self):
        for name in self.sources:
            self.replace_wheel_files(name, {"shared/__init__.py": b"VALUE = 1\n"})
        self.install_fixture()
        (self.installed / "shared/__init__.py").write_bytes(b"VALUE = 2\n")
        for name, paths in self.owned.items():
            self.record(name, paths)
        with (
            self.stage() as staged,
            self.assertRaisesRegex(DependencyObservationError, "verified wheel"),
        ):
            self.observe(staged)

    def test_identical_cross_wheel_scheme_collision_still_fails(self):
        self.replace_wheel_files("example", {"shared/file.txt": b"same"})
        self.replace_wheel_files(
            "helper", {"helper-1.0.data/purelib/shared/file.txt": b"same"}
        )
        self.install_fixture()
        with (
            self.stage() as staged,
            self.assertRaisesRegex(DependencyObservationError, "collide"),
        ):
            self.observe(staged)

    def test_symlink_payload_rejected(self):
        self.install_fixture()
        path = self.installed / "example/__init__.py"
        external = self.root / "external.py"
        external.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(external)
        except OSError:
            self.skipTest("Host does not permit test symlinks")
        with self.stage() as staged, self.assertRaises(DependencyObservationError):
            self.observe(staged)

    def test_installer_metadata_is_not_execution_provenance(self):
        self.install_fixture()
        path = self.installed / "example-1.0.dist-info/INSTALLER"
        path.write_bytes(b"unverified-installer\n")
        self.record("example", self.owned["example"])
        with self.stage() as staged, self.assertRaises(DependencyObservationError):
            self.observe(staged)

    def test_secure_record_hash_algorithms(self):
        self.install_fixture()
        for algorithm in ("sha384", "sha512"):
            with self.subTest(algorithm=algorithm):
                for name, paths in self.owned.items():
                    self.record(name, paths, algorithm)
                with self.stage() as staged:
                    self.observe(staged)

    def test_installed_directory_symlink_rejected(self):
        self.install_fixture()
        package = self.installed / "example"
        external = self.root / "external"
        package.rename(external)
        try:
            package.symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("Host does not permit test directory symlinks")
        with self.stage() as staged, self.assertRaises(DependencyObservationError):
            self.observe(staged)

    def test_installer_generated_scripts_require_projection(self):
        self.install_fixture()
        script = self.installed / "bin/console-tool"
        script.parent.mkdir()
        script.write_bytes(b"#!/usr/bin/python3\nimport example\n")
        with self.stage() as staged, self.assertRaises(DependencyObservationError):
            self.observe(staged)

    def test_file_count_size_and_record_bounds(self):
        self.install_fixture()
        for constant in (
            "_MAX_FILES",
            "_MAX_FILE_BYTES",
            "_MAX_TREE_BYTES",
            "_MAX_RECORD_BYTES",
        ):
            with (
                self.subTest(constant=constant),
                self.stage() as staged,
                patch(
                    "literate_ai.adapters.dependencies.python_installed." + constant, 1
                ),
                self.assertRaises(DependencyObservationError),
            ):
                self.observe(staged)

    def test_actual_offline_pip_installation(self):
        # Exercise real installer output without network, dependency solving,
        # package imports, global environment writes, or bytecode generation.
        environment = {
            key: os.environ[key]
            for key in ("PATH", "SystemRoot", "TEMP", "TMP")
            if key in os.environ
        }
        environment["PIP_CONFIG_FILE"] = os.devnull
        with self.stage() as staged:
            requirements = self.root / "locked.txt"
            requirements.write_text(
                "".join(
                    f"{p.name}=={p.version} --hash=sha256:{p.sha256}\n"
                    for p in staged.lock.packages
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "pip",
                    "--isolated",
                    "install",
                    "--no-index",
                    "--no-deps",
                    "--no-compile",
                    "--no-cache-dir",
                    "--disable-pip-version-check",
                    "--only-binary=:all:",
                    "--require-hashes",
                    "--find-links",
                    str(staged.directory),
                    "--target",
                    str(self.installed),
                    "-r",
                    str(requirements),
                ],
                env=environment,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = self.observe(staged)
        self.assertEqual(len(result.graph.components), 2)


if __name__ == "__main__":
    unittest.main()
