"""Native tree pkg topology and independently rehashed archive refusals."""

import hashlib
import io
import json
import lzma
import os
import platform
import subprocess
import tarfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters import freebsd_tree_packaging as tree_pkg
from literate_ai.adapters.freebsd_packaging import FreeBsdPkgToolBinding
from literate_ai.adapters.native_sdk_tree_custody import capture_native_sdk_tree
from literate_ai.adapters.tree_packaging import verify_tree_package_stage
from literate_ai.application.packaging import PackagingError
from literate_ai.contracts.executable_components.packages import (
    PackageFileKind,
    PackageInput,
    RuntimeRequirement,
    RuntimeRequirementKind,
)
from literate_ai.contracts.package_tree_inputs import PackageTreeInput
from tests.critical import test_tree_packages as tree_fixture


@unittest.skipIf(os.name == "nt", "Native tree pkg custody requires POSIX")
class FreeBsdTreePackageTests(unittest.TestCase):
    def setUp(self):
        fixture = tree_fixture.TreePackageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.store, self.work = fixture.store, fixture.work
        inputs = list(fixture.plan.base.inputs)
        for path, role, content in (
            (
                "provenance/component.md",
                "component-specification",
                b"Authored component specification",
            ),
            ("provenance/source.json", "cyclonedx-source-sbom", b'{"components":[]}'),
            (
                "provenance/resolved.json",
                "cyclonedx-resolved-sbom",
                b'{"components":[]}',
            ),
        ):
            blob = self.store.put_bytes(content)
            inputs.append(
                PackageInput(
                    path,
                    role,
                    PackageFileKind.RESOURCE,
                    fixture.plan.base.component_lock_identity,
                    fixture.plan.base.target_identity,
                    blob,
                )
            )
        self.plan = replace(
            fixture.plan,
            base=replace(
                fixture.plan.base,
                inputs=tuple(sorted(inputs, key=lambda item: item.path)),
            ),
        )
        self.tool = FreeBsdPkgToolBinding(
            Path("/unavailable/pkg-static"),
            "0" * 64,
            "2.8.4",
            "FreeBSD:16:amd64",
            "freebsd:16:x86:64",
        )
        self.projection = tree_pkg.project_freebsd_tree_package(
            self.plan,
            package="tree-pkg-fixture",
            version="0.0.0",
            origin="devel/tree-pkg-fixture",
            tool=self.tool,
            target_abi=self.tool.abi,
            resolved_sbom=b'{"components":[]}',
            prefix="/usr/local/lib/tree-pkg-fixture",
        )
        self.adapter = tree_pkg.FreeBsdTreePackageAdapter(self.projection, self.tool)

    def archive(self, mutate=None):
        members = self.projection.members
        records, directories = {}, {}
        data = {}
        for path, item in members.items():
            if item.kind == "directory":
                directories[path] = {"uname": "root", "gname": "wheel", "perm": "0755"}
                continue
            raw = (
                item.target.encode()
                if item.kind == "symlink"
                else self.store.get_bytes(item.blob)
                if item.kind == "file"
                else self.store.get_bytes(
                    members[self.projection.prefix + "/" + item.target].blob
                )
            )
            records[path] = {
                "sum": hashlib.sha256(raw).hexdigest(),
                "uname": "root",
                "gname": "wheel",
                "perm": f"{item.mode:04o}",
                "mtime": 946684800,
            }
            if item.kind == "symlink":
                records[path]["symlink_target"] = item.target
            data[path] = raw if item.kind == "file" else b""
        size = sum(
            item.blob.size
            if item.kind == "file"
            else len(item.target.encode())
            if item.kind == "symlink"
            else 0
            for item in members.values()
        )
        compact = {**self.projection.manifest, "flatsize": size}
        full = {**compact, "files": records, "directories": directories}
        entries = []
        for name, document in (("+COMPACT_MANIFEST", compact), ("+MANIFEST", full)):
            raw = json.dumps(document).encode()
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode, entry.uname, entry.gname = (
                len(raw),
                0o644,
                "root",
                "wheel",
            )
            entries.append((entry, raw))
        for path, item in members.items():
            entry = tarfile.TarInfo(path)
            entry.uid = entry.gid = 65534
            entry.uname, entry.gname, entry.mode, entry.mtime = (
                "root",
                "wheel",
                item.mode,
                946684800,
            )
            entry.type = {
                "file": tarfile.REGTYPE,
                "directory": tarfile.DIRTYPE,
                "symlink": tarfile.SYMTYPE,
                "hardlink": tarfile.LNKTYPE,
            }[item.kind]
            entry.linkname = (
                self.projection.prefix + "/" + item.target
                if item.kind == "hardlink"
                else item.target or ""
            )
            raw = data.get(path, b"")
            entry.size = len(raw)
            entries.append((entry, raw))
        if mutate:
            entries = mutate(entries)
        output = io.BytesIO()
        with tarfile.open(
            fileobj=output, mode="w", format=tarfile.PAX_FORMAT
        ) as archive:
            for entry, raw in entries:
                archive.addfile(entry, io.BytesIO(raw))
        return lzma.compress(output.getvalue())

    def verify(self, content):
        result = tree_pkg.FreeBsdTreePackageResult(
            self.plan.identity,
            self.adapter.identity,
            self.store.put_bytes(content, media_type="application/vnd.freebsd.pkg"),
        )
        self.adapter.verify(self.plan, result, store=self.store, object_root=self.work)

    def test_exact_native_tree_and_empty_directory_verify_without_native_tool(self):
        self.verify(self.archive())
        self.assertIn(
            "@dir /usr/local/lib/tree-pkg-fixture/native-sdk/empty",
            self.projection.plist.decode(),
        )
        self.assertEqual(
            self.projection.to_dict()["tree_package_plan"], self.plan.to_dict()
        )

    def test_rehashed_hardlink_copy_and_changed_symbolic_text_refuse(self):
        for kind in ("hardlink", "symlink"):

            def mutate(entries, kind=kind):
                selected = next(
                    entry
                    for entry, _ in entries
                    if entry.type
                    == (tarfile.LNKTYPE if kind == "hardlink" else tarfile.SYMTYPE)
                )
                if kind == "hardlink":
                    selected.type, selected.linkname, selected.size = (
                        tarfile.REGTYPE,
                        "",
                        8,
                    )
                    return [
                        (entry, b"compiler" if entry is selected else raw)
                        for entry, raw in entries
                    ]
                selected.linkname = "./" + selected.linkname
                return entries

            with self.subTest(kind=kind), self.assertRaises(PackagingError):
                self.verify(self.archive(mutate))

    def test_rehashed_manifest_hooks_targets_sizes_and_alias_checksums_refuse(self):
        for change in ("hook", "target", "size", "hardlink"):

            def mutate(entries, change=change):
                for index, (entry, raw) in enumerate(entries):
                    if entry.name != "+MANIFEST":
                        continue
                    value = json.loads(raw)
                    if change == "hook":
                        value["scripts"] = {"post-install": "exit 0"}
                    elif change == "target":
                        record = next(
                            record
                            for record in value["files"].values()
                            if "symlink_target" in record
                        )
                        record["symlink_target"] = "./" + record["symlink_target"]
                    elif change == "size":
                        value["flatsize"] += 8
                    else:
                        target = next(
                            entry.name
                            for entry, _ in entries
                            if entry.type == tarfile.LNKTYPE
                        )
                        value["files"][target]["sum"] = "0" * 64
                    raw = json.dumps(value).encode()
                    entry.size = len(raw)
                    entries[index] = entry, raw
                return entries

            with self.subTest(change=change), self.assertRaises(PackagingError):
                self.verify(self.archive(mutate))

    def test_compressed_trailer_and_extended_security_metadata_refuse(self):
        with self.assertRaises(PackagingError):
            self.verify(self.archive() + lzma.compress(b"undeclared frame"))

        def mutate(entries):
            entries[-1][0].pax_headers["SCHILY.xattr.security.capability"] = "unbound"
            return entries

        with self.assertRaises(PackagingError):
            self.verify(self.archive(mutate))

    def test_native_binary_charset_preserves_exact_path_and_target_checks(self):
        def binary(entries):
            for entry, _ in entries:
                if entry.type == tarfile.SYMTYPE:
                    entry.pax_headers["hdrcharset"] = "BINARY"
                elif entry.type == tarfile.DIRTYPE:
                    entry.pax_headers = {
                        "hdrcharset": "BINARY",
                        "path": entry.name + "/",
                    }
            return entries

        self.verify(self.archive(binary))
        for encoding in ("UTF-16", "unknown"):

            def invalid(entries, encoding=encoding):
                entries[-1][0].pax_headers["hdrcharset"] = encoding
                return entries

            with self.subTest(encoding=encoding), self.assertRaises(PackagingError):
                self.verify(self.archive(invalid))

        def changed_target(entries):
            for entry, _ in entries:
                if entry.type == tarfile.SYMTYPE:
                    entry.pax_headers = {
                        "hdrcharset": "BINARY",
                        "linkpath": "./" + entry.linkname,
                    }
            return entries

        with self.assertRaises(PackagingError):
            self.verify(self.archive(changed_target))

    def test_projection_requires_exact_bom_roles_and_target(self):
        with self.assertRaises(PackagingError):
            tree_pkg.project_freebsd_tree_package(
                self.plan,
                package="tree-pkg-fixture",
                version="0.0.0",
                origin="devel/tree-pkg-fixture",
                tool=self.tool,
                target_abi="FreeBSD:15:amd64",
                resolved_sbom=b'{"components":[]}',
                prefix=self.projection.prefix,
            )
        with (
            mock.patch.object(tree_pkg.platform, "system", return_value="Linux"),
            self.assertRaises(PackagingError),
        ):
            self.adapter.package(self.plan, store=self.store, object_root=self.work)

    def test_environment_obligation_remains_in_plan_without_fabricated_pkg_coordinate(
        self,
    ):
        requirement = RuntimeRequirement(
            "sdk-loader",
            RuntimeRequirementKind.ENVIRONMENT,
            "Fresh loader validation remains required",
            self.plan.base.component_lock_identity,
            False,
        )
        plan = replace(
            self.plan, base=replace(self.plan.base, runtime_requirements=(requirement,))
        )
        projection = tree_pkg.project_freebsd_tree_package(
            plan,
            package="tree-pkg-fixture",
            version="0.0.0",
            origin="devel/tree-pkg-fixture",
            tool=self.tool,
            target_abi=self.tool.abi,
            resolved_sbom=b'{"components":[]}',
            prefix=self.projection.prefix,
        )
        self.assertEqual(projection.dependencies, ())
        self.assertEqual(projection.plan.base.runtime_requirements, (requirement,))
        self.assertNotEqual(projection.identity, self.projection.identity)
        device = replace(requirement, kind=RuntimeRequirementKind.DEVICE)
        device_plan = replace(
            plan, base=replace(plan.base, runtime_requirements=(device,))
        )
        device_projection = tree_pkg.project_freebsd_tree_package(
            device_plan,
            package="tree-pkg-fixture",
            version="0.0.0",
            origin="devel/tree-pkg-fixture",
            tool=self.tool,
            target_abi=self.tool.abi,
            resolved_sbom=b'{"components":[]}',
            prefix=self.projection.prefix,
        )
        self.assertEqual(device_projection.dependencies, ())
        self.assertEqual(device_projection.plan.base.runtime_requirements, (device,))
        native_requirement = replace(
            requirement, kind=RuntimeRequirementKind.SHARED_LIBRARY
        )
        plan = replace(
            plan, base=replace(plan.base, runtime_requirements=(native_requirement,))
        )
        with self.assertRaisesRegex(PackagingError, "accepted coordinate"):
            tree_pkg.project_freebsd_tree_package(
                plan,
                package="tree-pkg-fixture",
                version="0.0.0",
                origin="devel/tree-pkg-fixture",
                tool=self.tool,
                target_abi=self.tool.abi,
                resolved_sbom=b'{"components":[]}',
                prefix=self.projection.prefix,
            )

    @unittest.skipUnless(platform.system() == "FreeBSD", "Native pkg requires FreeBSD")
    def test_actual_native_tool_preserves_tree_and_repeats_exact_bytes(self):
        source = self.work / "naming-source"
        (source / "bin").mkdir(parents=True)
        (source / "empty-é").mkdir()
        (source / "bin/CC").write_bytes(b"compiler bytes\n")
        (source / "bin/CC").chmod(0o755)
        os.link(source / "bin/CC", source / "bin/cc")
        (source / "public").symlink_to("bin", target_is_directory=True)
        (source / "é-headers").mkdir()
        long_name = "é-headers/é-" + "a" * 150 + ".txt"
        (source / long_name).write_bytes(b"Unicode payload\n")
        (source / "unicode-alias").symlink_to(long_name)
        tree = capture_native_sdk_tree(
            source, store=self.store, limits=self.plan.limits, case_sensitive=True
        )
        item = PackageTreeInput(
            "naming-matrix",
            "native-naming-fixture",
            self.plan.base.component_lock_identity,
            self.plan.base.target_identity,
            tree,
        )
        plan = replace(
            self.plan,
            trees=tuple(
                sorted((*self.plan.trees, item), key=lambda value: value.prefix)
            ),
            case_sensitive=True,
        )
        tool = FreeBsdPkgToolBinding.discover()
        projection = replace(
            self.projection,
            plan=plan,
            tool_identity=tool.identity,
            abi=tool.abi,
            architecture=tool.architecture,
        )
        adapter = tree_pkg.FreeBsdTreePackageAdapter(projection, tool)
        first = adapter.package(plan, store=self.store, object_root=self.work)
        second = adapter.package(plan, store=self.store, object_root=self.work)
        self.assertEqual(first, second)
        adapter.verify(plan, first, store=self.store, object_root=self.work)
        archive = self.work / "native.pkg"
        self.store.copy_to(first.archive, archive)
        target = self.work / "unpacked"
        target.mkdir()
        result = subprocess.run(
            ["/usr/bin/tar", "-xpf", str(archive), "-C", str(target)],
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        root = target / projection.prefix[1:]
        verify_tree_package_stage(plan, root)
        retained = root / item.prefix
        self.assertEqual(
            (retained / "bin/CC").stat().st_ino, (retained / "bin/cc").stat().st_ino
        )
        self.assertEqual(os.readlink(retained / "unicode-alias"), long_name)
        self.assertTrue((retained / "empty-é").is_dir())
