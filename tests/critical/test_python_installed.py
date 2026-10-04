from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

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

    def observe(self, staged):
        return observe_python_installation(staged, self.installed, scheme=self.scheme)

    def test_payload_change_cannot_be_hidden_by_rewriting_record(self):
        self.install_fixture()
        (self.installed / "example/__init__.py").write_bytes(b"modified = True\n")
        self.record("example", self.owned["example"])
        with (
            self.stage() as staged,
            self.assertRaisesRegex(DependencyObservationError, "verified wheel"),
        ):
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


if __name__ == "__main__":
    unittest.main()
