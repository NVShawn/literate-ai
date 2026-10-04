"""Debian projection and independently inspected native-package tests."""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import subprocess
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters.debian_packaging import (
    DebianPackageAdapter,
    DpkgDebToolBinding,
    project_debian_package,
)
from literate_ai.adapters.packaging import native_archive_package_plan
from literate_ai.application.packaging import PackagingError
from literate_ai.contracts import BlobRef
from literate_ai.contracts.executable_components.packages import PackageKind
from tests.support import fixtures_test_package_adapters as package_adapter_fixtures
from tests.support.fixtures_test_package_release_contracts import _identity


def _tar(entries: dict[str, tuple[bytes, int]]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:", format=tarfile.GNU_FORMAT) as archive:
        directories = {"."}
        for name in entries:
            parts = name.split("/")[:-1]
            directories.update(
                "/".join(parts[:index]) for index in range(1, len(parts) + 1)
            )
        for name in sorted(directories):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.uid = info.gid = 0
            info.mtime = 946684800
            archive.addfile(info)
        for name, (body, mode) in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = mode
            info.uid = info.gid = 0
            info.mtime = 946684800
            archive.addfile(info, io.BytesIO(body))
    compressed = io.BytesIO()
    with gzip.GzipFile(
        fileobj=compressed, mode="wb", filename="", mtime=946684800
    ) as stream:
        stream.write(raw.getvalue())
    return compressed.getvalue()


def _ar(members: dict[str, bytes]) -> bytes:
    result = bytearray(b"!<arch>\n")
    for name, body in members.items():
        header = (
            f"{name + '/':<16}{946684800:<12}{0:<6}{0:<6}{'100644':<8}"
            f"{len(body):<10}`\n"
        ).encode("ascii")
        assert len(header) == 60
        result.extend(header)
        result.extend(body)
        if len(body) % 2:
            result.extend(b"\n")
    return bytes(result)


class DebianPackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = package_adapter_fixtures.PackageAdapterTests()
        fixture.setUp()
        self.fixture = fixture
        self.contents = dict(fixture.contents)
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        executable = root / "dpkg-deb"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        self.tool = DpkgDebToolBinding(
            (str(executable),),
            executable.resolve(),
            "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest(),
            "Debian 'dpkg-deb' package archive backend version 1.22.6",
        )
        specification = b"# Exact Component\n"
        sbom = b'{"bomFormat":"CycloneDX","components":[]}'
        base = fixture.fixture.plan(PackageKind.ARCHIVE, ())
        self.plan = native_archive_package_plan(
            base,
            packager_identity=base.packager_identity,
            specification=specification,
            resolved_sbom=sbom,
            resolved_sbom_source_identity=_identity("resolved-sbom"),
        )
        for item in self.plan.inputs:
            if item.path == "specification/component.md":
                self.contents[item.blob.identity] = specification
            elif item.path == "sbom/resolved.cdx.json":
                self.contents[item.blob.identity] = sbom
        self.projection = project_debian_package(
            self.plan,
            package="sample-app",
            version="1.2.0",
            target_architecture="x86_64",
            worker_architecture="x86_64",
            resolved_sbom=sbom,
            tool_identity=self.tool.identity,
        )
        self.fake_projection = self.projection
        self.adapter = DebianPackageAdapter(self.projection, self.tool)
        self.plan = replace(self.plan, packager_identity=self.adapter.packager_identity)
        self.input_root = root / "inputs"
        for item in self.plan.inputs:
            destination = self.input_root / item.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(self.contents[item.blob.identity])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _fake_dpkg(self, arguments, **_kwargs):
        root = Path(arguments[-2])
        output = Path(arguments[-1])
        projected_modes = {
            item.installed_path.removeprefix("/"): item.mode
            for item in self.fake_projection.payload
        }
        data = {}
        for path in root.rglob("*"):
            if path.is_file() and "DEBIAN" not in path.parts:
                relative = path.relative_to(root).as_posix()
                data[f"./{relative}"] = (
                    path.read_bytes(),
                    projected_modes[relative],
                )
        content = _ar(
            {
                "debian-binary": b"2.0\n",
                "control.tar.gz": _tar(
                    {"./control": ((root / "DEBIAN/control").read_bytes(), 0o644)}
                ),
                "data.tar.gz": _tar(data),
            }
        )
        output.write_bytes(content)
        return subprocess.CompletedProcess(arguments, 0, "built\n", "")

    @staticmethod
    def _result_for_content(result, content: bytes):
        blob = BlobRef(
            hashlib.sha256(content).hexdigest(),
            len(content),
            media_type="application/vnd.debian.binary-package",
        )
        return replace(result, artifacts=(replace(result.artifacts[0], blob=blob),))

    def test_real_deb_boundary_is_reproducible_and_independently_verified(self) -> None:
        native_utime = os.utime

        def windows_compatible_utime(path, times, *, follow_symlinks=True):
            if not follow_symlinks:
                raise NotImplementedError
            return native_utime(path, times)

        with (
            mock.patch(
                "literate_ai.adapters.debian_packaging.platform.system",
                return_value="Linux",
            ),
            mock.patch(
                "literate_ai.adapters.debian_packaging.run_with_tree_kill",
                side_effect=self._fake_dpkg,
            ) as process,
            mock.patch(
                "literate_ai.adapters.debian_packaging.os.utime",
                side_effect=windows_compatible_utime,
            ),
        ):
            first = self.adapter.package(
                self.plan,
                materialized_root=self.input_root,
                object_root=Path(self.temporary.name) / "objects",
            )
            first_bytes = self.adapter.read_created_blob(first.artifacts[0].blob)
            second = self.adapter.package(
                self.plan,
                materialized_root=self.input_root,
                object_root=Path(self.temporary.name) / "objects",
            )
            second_bytes = self.adapter.read_created_blob(second.artifacts[0].blob)
        self.assertEqual(first_bytes, second_bytes)
        self.assertTrue(first.artifacts[0].path.endswith("_amd64.deb"))
        arguments = process.call_args_list[0].args[0]
        self.assertIn("--root-owner-group", arguments)
        self.assertIn("--uniform-compression", arguments)
        self.assertIn("-Zgzip", arguments)

        tampered = bytearray(first_bytes)
        tampered[-20] ^= 1
        with self.assertRaises(PackagingError):
            self.adapter.verify_bytes(first, bytes(tampered))

        members = {
            "debian-binary": b"2.0\n",
            "control.tar.gz": _tar(
                {
                    "./control": (self.projection.control, 0o644),
                    "./postinst": (b"#!/bin/sh\n", 0o755),
                }
            ),
            "data.tar.gz": _tar({"../escape": (b"bad", 0o644)}),
        }
        unsafe = _ar(members)
        with self.assertRaisesRegex(PackagingError, "control archive"):
            self.adapter.verify_bytes(self._result_for_content(first, unsafe), unsafe)
        traversal = _ar(
            {
                "debian-binary": b"2.0\n",
                "control.tar.gz": _tar({"./control": (self.projection.control, 0o644)}),
                "data.tar.gz": _tar({"../escape": (b"bad", 0o644)}),
            }
        )
        with self.assertRaisesRegex(PackagingError, "unsafe path"):
            self.adapter.verify_bytes(
                self._result_for_content(first, traversal), traversal
            )


if __name__ == "__main__":
    unittest.main()
