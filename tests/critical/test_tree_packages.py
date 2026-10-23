"""Real link-preserving TARs and independent refusal of rehashed corruptions."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path, PurePosixPath
from unittest import mock

from literate_ai.adapters import tree_packaging
from literate_ai.adapters.native_sdk_tree_custody import capture_native_sdk_tree
from literate_ai.application.packaging import PackagingError
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.executable_components.packages import (
    PackageKind,
    PackagePlan,
)
from literate_ai.contracts.native_sdk_trees import NativeSdkAlias
from literate_ai.contracts.package_tree_inputs import PackageTreeInput
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.contracts.tree_packages import TreePackagePlan, TreePackageResult
from literate_ai.storage.cas import FileSystemCAS
from tests.support import fixtures_test_package_adapters as fixtures


@unittest.skipIf(os.name == "nt", "Tree TAR custody requires POSIX")
class TreePackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tree-tar-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.store = FileSystemCAS(self.base / "store")
        self.work = self.base / "work"
        self.work.mkdir()
        fixture = fixtures.PackageAdapterTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.adapter = tree_packaging.TreeTarPackageAdapter()
        base = replace(
            fixture.fixture.plan(PackageKind.ARCHIVE, ()),
            packager_identity=self.adapter.identity,
        )
        for item in base.inputs:
            self.assertEqual(
                self.store.put_bytes(
                    fixture.read(item.blob), media_type=item.blob.media_type
                ),
                item.blob,
            )
        source = self.base / "source"
        (source / "bin").mkdir(parents=True)
        (source / "empty").mkdir()
        (source / "bin/cc").write_bytes(b"compiler")
        (source / "bin/cc").chmod(0o755)
        os.link(source / "bin/cc", source / "bin/compiler")
        (source / "public").symlink_to("bin", target_is_directory=True)
        self.limits = RetainedProjectLimits(100, 65536, 1048576)
        tree = capture_native_sdk_tree(source, store=self.store, limits=self.limits)
        value = PackageTreeInput(
            "native-sdk",
            "native-sdk-tree",
            canonical_identity("source"),
            base.target_identity,
            tree,
        )
        self.plan = TreePackagePlan(base, (value,), self.limits)
        shutil.rmtree(source)

    def package(self):
        return self.adapter.package(self.plan, store=self.store, object_root=self.work)

    def verify(self, result):
        return self.adapter.verify(
            self.plan, result, store=self.store, object_root=self.work
        )

    def alter(self, result, mutate):
        source = self.store.get_bytes(result.archive)
        output = io.BytesIO()
        with (
            tarfile.open(fileobj=io.BytesIO(source), mode="r:") as archive,
            tarfile.open(
                fileobj=output, mode="w", format=tarfile.PAX_FORMAT
            ) as changed,
        ):
            for member in archive:
                data = archive.extractfile(member).read() if member.isreg() else None
                member, data = mutate(member, data)
                changed.addfile(member, io.BytesIO(data) if data is not None else None)
        return replace(
            result,
            archive=self.store.put_bytes(
                output.getvalue(), media_type="application/x-tar"
            ),
        )

    def test_plan_roundtrip_and_combined_bounds_preserve_legacy_wire(self):
        self.assertEqual(TreePackagePlan.from_dict(self.plan.to_dict()), self.plan)
        self.assertEqual(
            PackagePlan.from_dict(self.plan.base.to_dict()), self.plan.base
        )
        with self.assertRaises(ValueError):
            PackagePlan.from_dict(self.plan.to_dict())
        for limits in (
            replace(self.limits, max_entries=1),
            replace(self.limits, max_total_bytes=1),
        ):
            with self.assertRaisesRegex(ValueError, "bound"):
                replace(self.plan, limits=limits)
        with self.assertRaisesRegex(ValueError, "ownership"):
            replace(self.plan, trees=(replace(self.plan.trees[0], prefix="bin"),))

    def test_repeatable_archive_extracts_real_links_with_system_tar(self):
        first = self.package()
        second = self.package()
        self.assertEqual(first, second)
        self.assertEqual(TreePackageResult.from_dict(first.to_dict()), first)
        self.assertEqual(self.verify(first), first)
        archive = self.base / "package.tar"
        self.store.copy_to(first.archive, archive)
        extracted = self.base / "extracted"
        extracted.mkdir()
        command = shutil.which("tar")
        if command is None:
            self.fail("POSIX package acceptance requires a native tar extractor")
        result = subprocess.run(
            [command, "-xpf", str(archive), "-C", str(extracted)],
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        tree_packaging.verify_tree_package_stage(self.plan, extracted)
        self.assertEqual(
            (extracted / "native-sdk/bin/cc").stat().st_ino,
            (extracted / "native-sdk/bin/compiler").stat().st_ino,
        )
        self.assertEqual(
            (extracted / "native-sdk/public/compiler").read_bytes(), b"compiler"
        )
        self.assertTrue((extracted / "native-sdk/empty").is_dir())
        self.assertEqual(list(self.work.iterdir()), [])

    def test_rehashed_hardlink_flattening_and_symbolic_target_substitution_refuse(self):
        original = self.package()

        def flatten(member, data):
            if member.islnk():
                member.type = tarfile.REGTYPE
                member.linkname = ""
                data = b"compiler"
                member.size = len(data)
            return member, data

        with self.assertRaisesRegex(PackagingError, "topology"):
            self.verify(self.alter(original, flatten))

        def escape(member, data):
            if member.issym():
                member.linkname = "../../outside"
            return member, data

        with self.assertRaisesRegex(PackagingError, "target"):
            self.verify(self.alter(original, escape))

    def test_rehashed_bytes_modes_and_nonzero_trailer_refuse(self):
        original = self.package()

        def corrupt(member, data):
            if member.name == "native-sdk/bin/cc":
                data = b"corrupt!"
            return member, data

        with self.assertRaisesRegex(PackagingError, "bytes"):
            self.verify(self.alter(original, corrupt))

        def mode(member, data):
            if member.name == "native-sdk/bin/cc":
                member.mode = 0o644
            return member, data

        with self.assertRaisesRegex(PackagingError, "metadata"):
            self.verify(self.alter(original, mode))
        content = self.store.get_bytes(original.archive)
        with self.assertRaisesRegex(PackagingError, "trailer"):
            self.verify(
                replace(
                    original,
                    archive=self.store.put_bytes(
                        content[:-1] + b"x", media_type="application/x-tar"
                    ),
                )
            )

    def test_oversized_pax_read_refuses_before_metadata_allocation(self):
        header = tarfile.TarInfo("pax")
        header.type = tarfile.XHDTYPE
        header.size = 2 * 1024 * 1024
        content = header.tobuf(format=tarfile.USTAR_FORMAT) + b"\0" * (
            header.size + 1024
        )
        result = TreePackageResult(
            self.plan.identity,
            self.adapter.identity,
            self.store.put_bytes(content, media_type="application/x-tar"),
        )
        with self.assertRaisesRegex(PackagingError, "allocation bound"):
            self.verify(result)

    def test_case_sensitive_names_are_preserved_or_destination_refuses(self):
        original = self.plan.trees[0]
        tree = original.tree
        physical = replace(tree.files[0], path="bin/CC")
        aliases = tuple(
            sorted(
                (
                    NativeSdkAlias("bin/cc", "bin/CC", "hardlink"),
                    *(
                        replace(v, target="bin/CC") if v.kind == "hardlink" else v
                        for v in tree.aliases
                    ),
                ),
                key=lambda v: v.path,
            )
        )
        selected = replace(
            original,
            tree=replace(tree, files=(physical,), aliases=aliases, case_sensitive=True),
        )
        self.plan = replace(self.plan, trees=(selected,), case_sensitive=True)
        probe = self.base / "CaseProbe"
        probe.write_bytes(b"")
        supported = not (self.base / "caseprobe").exists()
        probe.unlink()
        if supported:
            result = self.package()
            self.verify(result)
            with tarfile.open(
                fileobj=io.BytesIO(self.store.get_bytes(result.archive)), mode="r:"
            ) as archive:
                names = archive.getnames()
            self.assertIn("native-sdk/bin/CC", names)
            self.assertIn("native-sdk/bin/cc", names)
        else:
            with self.assertRaisesRegex(ValueError, "case-sensitive"):
                self.package()
            self.assertEqual(list(self.work.iterdir()), [])

    def test_pax_long_unicode_path_retains_exact_tree_names(self):
        value = self.plan.trees[0]
        name = "include/" + "directory/" * 20 + "héader.h"
        blob = self.store.put_bytes(b"header bytes")
        item = replace(value.tree.files[0], path=name, blob=blob, executable=False)
        directories = set(value.tree.directories)
        directories.update(str(p) for p in PurePosixPath(name).parents if str(p) != ".")
        tree = replace(
            value.tree,
            directories=tuple(sorted(directories)),
            files=tuple(sorted((*value.tree.files, item), key=lambda v: v.path)),
        )
        self.plan = replace(self.plan, trees=(replace(value, tree=tree),))
        result = self.package()
        self.verify(result)
        with tarfile.open(
            fileobj=io.BytesIO(self.store.get_bytes(result.archive)), mode="r:"
        ) as archive:
            member = archive.getmember(value.prefix + "/" + name)
            self.assertEqual(member.pax_headers["path"], member.name)
            self.assertEqual(archive.extractfile(member).read(), b"header bytes")

    def test_stage_topology_drift_and_failed_creation_preserve_siblings(self):
        root = tree_packaging.materialize_tree_package_plan(
            self.plan,
            expected_identity=self.plan.identity,
            store=self.store,
            parent=self.work,
        )
        path = root / "native-sdk/bin/compiler"
        path.unlink()
        path.write_bytes(b"compiler")
        path.chmod(0o755)
        with self.assertRaisesRegex(PackagingError, "topology"):
            tree_packaging.verify_tree_package_stage(self.plan, root)
        sibling = self.work / "keep"
        sibling.write_bytes(b"sentinel")
        before = set(self.work.iterdir())
        with mock.patch.object(
            tree_packaging,
            "materialize_package_tree_input",
            side_effect=OSError("copy failed"),
        ):
            with self.assertRaisesRegex(OSError, "copy failed"):
                tree_packaging.materialize_tree_package_plan(
                    self.plan,
                    expected_identity=self.plan.identity,
                    store=self.store,
                    parent=self.work,
                )
        self.assertEqual(set(self.work.iterdir()), before)
        self.assertEqual(sibling.read_bytes(), b"sentinel")
        with self.assertRaisesRegex(PackagingError, "provider"):
            self.adapter.package(
                replace(
                    self.plan,
                    base=replace(
                        self.plan.base,
                        packager_identity=canonical_identity("other provider"),
                    ),
                ),
                store=self.store,
                object_root=self.work,
            )


if __name__ == "__main__":
    unittest.main()
