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
from literate_ai.adapters.builders import BuildError
from literate_ai.adapters.native_sdk_tree_custody import capture_native_sdk_tree
from literate_ai.adapters.native_tar_archives import (
    NativeTarToolBinding,
    rewrite_native_tar,
)
from literate_ai.adapters.native_tree_shards import (
    prepare_native_tree,
    retain_native_tree,
)
from literate_ai.adapters.retained_unit_graphs import (
    prepare_retained_unit_graph,
    read_retained_unit_graph,
    retain_retained_unit_graph,
)
from literate_ai.application.packaging import PackagingError
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.composed_tree_packages import ComposedTreePackageProjection
from literate_ai.contracts.executable_components.packages import (
    PackageKind,
    PackagePlan,
)
from literate_ai.contracts.identity import ComponentCoordinate
from literate_ai.contracts.native_sdk_trees import NativeSdkAlias
from literate_ai.contracts.package_tree_inputs import PackageTreeInput
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.contracts.retained_unit_packages import (
    RetainedUnitDependency,
    RetainedUnitOwnership,
    RetainedUnitPackageDefinition,
)
from literate_ai.contracts.retained_unit_partition import RetainedUnitPartition
from literate_ai.contracts.tree_packages import TreePackagePlan, TreePackageResult
from literate_ai.storage.cas import FileSystemCAS
from tests.support import fixtures_test_package_adapters as fixtures


@unittest.skipIf(os.name == "nt", "Tree TAR custody requires POSIX")
class TreePackageTests(unittest.TestCase):
    def test_native_libarchive_preserves_external_links_and_bounds_file_output(self):
        command = shutil.which("bsdtar")
        if command is None:
            self.skipTest(
                "Native libarchive bsdtar unavailable; no rewrite qualification"
            )
        tool = NativeTarToolBinding.discover(command)
        source = self.work / "explicit-links.tar"
        with tarfile.open(source, "w", format=tarfile.PAX_FORMAT) as archive:
            directory = tarfile.TarInfo("bin")
            directory.type, directory.mode = tarfile.DIRTYPE, 0o755
            archive.addfile(directory)
            hardlink = tarfile.TarInfo("bin/compiler")
            hardlink.type, hardlink.linkname, hardlink.mode = (
                tarfile.LNKTYPE,
                "bin/cc",
                0o755,
            )
            archive.addfile(hardlink)
            symlink = tarfile.TarInfo("public")
            symlink.type, symlink.linkname, symlink.mode = tarfile.SYMTYPE, "bin", 0o777
            archive.addfile(symlink)
        reference = self.store.put_file(source, media_type="application/x-tar")
        output = self.work / "native-links.txz"
        with output.open("x+b") as sink:
            rewrite_native_tar(
                tool,
                source,
                reference,
                sink,
                maximum_input_bytes=65536,
                maximum_output_bytes=65536,
            )
        with tarfile.open(output, "r:xz") as archive:
            entries = {entry.name: entry for entry in archive}
        self.assertEqual(set(entries), {"bin", "bin/compiler", "public"})
        self.assertEqual(
            (
                entries["bin/compiler"].type,
                entries["bin/compiler"].linkname,
                entries["bin/compiler"].size,
            ),
            (tarfile.LNKTYPE, "bin/cc", 0),
        )
        self.assertEqual(
            (entries["public"].type, entries["public"].linkname),
            (tarfile.SYMTYPE, "bin"),
        )
        preserved = output.read_bytes()
        # Public construction emits exactly one XZ frame, without libarchive's
        # stdout record padding that a permissive tarfile reader would ignore.
        import lzma

        decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=128 * 1024**2)
        decoder.decompress(preserved, max_length=65536)
        self.assertTrue(decoder.eof)
        self.assertEqual(decoder.unused_data, b"")
        refused = self.work / "over-budget.txz"
        with refused.open("x+b") as sink:
            with self.assertRaisesRegex(BuildError, "stdout or stderr byte budget"):
                rewrite_native_tar(
                    tool,
                    source,
                    reference,
                    sink,
                    maximum_input_bytes=65536,
                    maximum_output_bytes=16,
                )
        self.assertLessEqual(refused.stat().st_size, 16)
        self.assertEqual(output.read_bytes(), preserved)
        with (self.work / "text-output").open("x") as sink:
            with self.assertRaisesRegex(PackagingError, "binary file"):
                rewrite_native_tar(
                    tool,
                    source,
                    reference,
                    sink,
                    maximum_input_bytes=65536,
                    maximum_output_bytes=65536,
                )

        class FailedArchiveSink(io.BufferedRandom):
            def write(self, content):
                raise TypeError("injected external file-write failure")

        with FailedArchiveSink(
            (self.work / "write-failure").open("x+b", buffering=0)
        ) as sink:
            with self.assertRaisesRegex(BuildError, "output could not be read"):
                rewrite_native_tar(
                    tool,
                    source,
                    reference,
                    sink,
                    maximum_input_bytes=65536,
                    maximum_output_bytes=65536,
                )

        class ShortArchiveSink(io.BufferedRandom):
            def write(self, content):
                return super().write(content[:7])

        short = self.work / "short-writes.txz"
        with ShortArchiveSink(short.open("x+b", buffering=0)) as sink:
            rewrite_native_tar(
                tool,
                source,
                reference,
                sink,
                maximum_input_bytes=65536,
                maximum_output_bytes=65536,
            )
        self.assertEqual(short.read_bytes(), preserved)
        self.assertEqual(output.read_bytes(), preserved)

    def test_composed_cross_unit_links_never_copy_dependency_payload(self):
        old = self.plan.trees[0].tree
        tree = prepare_native_tree(
            old.directories, old.files, old.aliases, limits=self.limits
        )
        parameters = BlobRef(
            canonical_identity({}).digest, 2, media_type="application/json"
        )

        def definition(name):
            provenance = {
                field: canonical_identity(field)
                for field in RetainedUnitPackageDefinition.IDENTITY_FIELDS
            }
            provenance["payload_identity"] = tree.identity
            return RetainedUnitPackageDefinition(
                name,
                ComponentCoordinate("test", name),
                **provenance,
                provider_parameters=parameters,
                abi="FreeBSD:14:amd64",
                version="1.0",
            )

        physical, aliases = definition("physical"), definition("aliases")
        partition = RetainedUnitPartition(
            tree,
            tuple(sorted((physical, aliases), key=lambda value: value.identity.uri)),
            (
                RetainedUnitOwnership("bin/cc", "file", physical.identity),
                RetainedUnitOwnership("bin/compiler", "hardlink", aliases.identity),
                RetainedUnitOwnership("empty", "empty-directory", physical.identity),
                RetainedUnitOwnership("public", "symlink", aliases.identity),
            ),
            (
                RetainedUnitDependency(
                    aliases.identity,
                    physical.identity,
                    "tree-link",
                    physical.target_identity,
                ),
            ),
            self.limits,
            10,
            20,
            65536,
        )
        tree_reference = retain_native_tree(tree, self.store)
        graph = prepare_retained_unit_graph(
            partition,
            tree_reference,
            max_descriptor_bytes=2400,
            max_descriptor_total_bytes=65536,
            max_index_bytes=4096,
            max_metadata_memory_bytes=1024 * 1024,
        )
        reference = retain_retained_unit_graph(graph, self.store)

        def reopen_graph(reference=reference, expected=graph.identity):
            return read_retained_unit_graph(
                reference,
                self.store,
                expected_identity=expected,
                tree=tree,
                per_unit_limits=self.limits,
                max_units=partition.max_units,
                max_dependencies=partition.max_dependencies,
                max_metadata_bytes=partition.max_metadata_bytes,
                max_descriptor_bytes=2400,
                max_descriptor_total_bytes=65536,
                max_index_bytes=4096,
                max_metadata_memory_bytes=1024 * 1024,
            )

        projection = ComposedTreePackageProjection(
            reopen_graph().partition, 1000, 1024 * 1024
        )
        for mask in (0o002, 0o077, 0o022):
            with self.subTest(umask=oct(mask)):
                previous_mask = os.umask(mask)
                try:
                    stage = tree_packaging.materialize_composed_tree_package_set(
                        projection,
                        expected_tree_identity=tree.identity,
                        store=self.store,
                        parent=self.work,
                    )
                finally:
                    os.umask(previous_mask)
                for member in projection.members:
                    if member.kind == "directory":
                        self.assertEqual(
                            (stage / member.path).stat().st_mode & 0o7777,
                            member.mode,
                        )
                (stage / "empty").chmod(0o700)
                with self.assertRaisesRegex(PackagingError, "directory mode changed"):
                    tree_packaging.verify_composed_tree_package_stage(projection, stage)
                (stage / "empty").chmod(0o755)
                tree_packaging.verify_composed_tree_package_stage(projection, stage)
        self.assertEqual((stage / "public/compiler").read_bytes(), b"compiler")
        self.assertEqual(
            (stage / "bin/cc").stat().st_ino, (stage / "bin/compiler").stat().st_ino
        )
        self.assertTrue((stage / "empty").is_dir())
        subset = {
            value.path: value for value in projection.members_for(aliases.identity)
        }
        self.assertEqual(set(subset), {"bin", "bin/compiler", "public"})
        self.assertEqual(subset["bin/compiler"].target, "bin/cc")
        self.assertIsNone(subset["bin/compiler"].blob)
        self.assertEqual(subset["public"].target, "bin")
        with self.assertRaisesRegex(ValueError, "tree-link-dependency"):
            ComposedTreePackageProjection(
                replace(partition, dependencies=()), 1000, 1024 * 1024
            )
        with self.assertRaisesRegex(ValueError, "exact-output-partition"):
            replace(partition, ownership=partition.ownership[:-1])
        # Understated graph counts must refuse before the missing next group's
        # descriptor is read. Prior usable staging remains physically intact.
        understated = replace(graph.index, ownership_count=1)
        altered = self.store.put_manifest(
            understated.to_dict(), media_type="application/json"
        )
        self.store.path_for(graph.index.dependencies[0]).unlink()
        with self.assertRaisesRegex(
            ValueError, "actual-graph-count-exceeds-declaration"
        ):
            reopen_graph(altered, understated.identity)
        self.assertEqual(
            (stage / "bin/cc").stat().st_ino, (stage / "bin/compiler").stat().st_ino
        )
        # A separately copied target has equal bytes but has lost admitted inode
        # topology. The whole-set verifier must still reject that staging tree.
        (stage / "bin/compiler").unlink()
        (stage / "bin/compiler").write_bytes(b"compiler")
        (stage / "bin/compiler").chmod(0o755)
        with self.assertRaisesRegex(PackagingError, "topology"):
            tree_packaging.verify_composed_tree_package_stage(projection, stage)

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
