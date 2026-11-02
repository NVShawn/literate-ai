"""Native tree pkg topology and independently rehashed archive refusals."""

import copy
import hashlib
import io
import json
import lzma
import os
import platform
import subprocess
import sys
import tarfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from literate_ai.adapters import freebsd_tree_packaging as tree_pkg
from literate_ai.adapters.action_blob_source import HttpActionBlobSource
from literate_ai.adapters.action_command_blob_source import CommandActionBlobSource
from literate_ai.adapters.action_dispatch_wire import ActionDispatchDeadline
from literate_ai.adapters.action_filesystem_blob_source import (
    FileSystemActionBlobSource,
)
from literate_ai.adapters.freebsd_composed_tree_packaging import (
    inspect_freebsd_composed_package_set,
    verify_freebsd_composed_package_set,
)
from literate_ai.adapters.freebsd_packaging import FreeBsdPkgToolBinding
from literate_ai.adapters.native_sdk_tree_custody import capture_native_sdk_tree
from literate_ai.adapters.retained_unit_results import (
    read_retained_unit_result_index,
    read_retained_unit_results,
    retain_retained_unit_results,
)
from literate_ai.adapters.retained_unit_transfer import (
    receive_retained_unit_package_set,
)
from literate_ai.adapters.retained_unit_verification import (
    RetainedUnitVerificationSelection,
)
from literate_ai.adapters.tree_packaging import verify_tree_package_stage
from literate_ai.application.packaging import PackagingError
from literate_ai.contracts import BlobRef, canonical_identity, canonical_json_bytes
from literate_ai.contracts.executable_components.packages import (
    PackageFileKind,
    PackageInput,
    PackageKind,
    RuntimeRequirement,
    RuntimeRequirementKind,
)
from literate_ai.contracts.execution_dispatch import (
    LIFECYCLE_ACTION_WIRE_PROTOCOL,
    ExecutionWorker,
    ExecutionWorkerKind,
)
from literate_ai.contracts.native_sdk_trees import NativeSdkAlias, NativeSdkTree
from literate_ai.contracts.native_sdks import NativeSdkFile
from literate_ai.contracts.package_tree_inputs import PackageTreeInput
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.contracts.retained_unit_policy import RetainedUnitPackagePolicy
from literate_ai.contracts.retained_unit_results import RetainedUnitPackageResultShard
from literate_ai.contracts.tree_packages import TreePackagePlan, TreePackageResult
from literate_ai.return_cas import CONFIG_SCHEMA
from literate_ai.storage.cas import BlobIntegrityError, FileSystemCAS
from tests.critical import test_tree_packages as tree_fixture
from tests.support import fixtures_test_package_adapters as package_fixture
from tests.support.action_deadline import ACTION_TEST_DEADLINE
from tests.support.fixtures_retained_unit_archives import (
    archive_assertion,
    composed_fixture,
)
from tests.support.fixtures_test_action_blob_source import blob_path, source_cas_server
from tests.support.fixtures_test_executable_component_v2_schemas import (
    _official_validator,
)
from tests.support.fixtures_test_freebsd_package_elf import freebsd_package_elf


class FreeBsdTreePackageCodecTests(unittest.TestCase):
    """Wire decoding is portable and grants no filesystem or producer authority."""

    def setUp(self):
        fixture = package_fixture.PackageAdapterTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        base = fixture.fixture.plan(PackageKind.ARCHIVE, ())
        inputs = list(base.inputs)
        for name, role in (
            ("component.md", "component-specification"),
            ("source.json", "cyclonedx-source-sbom"),
            ("resolved.json", "cyclonedx-resolved-sbom"),
        ):
            inputs.append(
                PackageInput(
                    "provenance/" + name,
                    role,
                    PackageFileKind.RESOURCE,
                    base.component_lock_identity,
                    base.target_identity,
                    BlobRef("1" * 64, 8),
                )
            )
        base = replace(base, inputs=tuple(sorted(inputs, key=lambda v: v.path)))
        tree = NativeSdkTree(
            ("bin", "empty"),
            (NativeSdkFile("bin/CC", BlobRef("2" * 64, 8), True),),
            (
                NativeSdkAlias("bin/cc", "bin/CC", "hardlink"),
                NativeSdkAlias("public", "bin"),
            ),
            case_sensitive=True,
        )
        self.plan = TreePackagePlan(
            base,
            (
                PackageTreeInput(
                    "sdk",
                    "native-sdk-tree",
                    canonical_identity("source"),
                    base.target_identity,
                    tree,
                ),
            ),
            RetainedProjectLimits(100, 65536, 1048576),
            True,
        )
        self.projection = tree_pkg.FreeBsdTreePackageProjection(
            self.plan,
            "codec-fixture",
            "1.0.0",
            "devel/codec-fixture",
            "FreeBSD:16:amd64",
            "freebsd:16:x86:64",
            "/usr/local/lib/codec",
            (("dependency", "devel/dependency", "1.0.0"),),
            canonical_identity("tool"),
        )
        self.result = tree_pkg.FreeBsdTreePackageResult(
            self.plan.identity,
            canonical_identity("packager"),
            BlobRef("3" * 64, 128, media_type="application/vnd.freebsd.pkg"),
        )

    def test_native_contracts_roundtrip_with_schema_and_exact_identity(self):
        for value in (self.projection, self.result):
            with self.subTest(schema=value.SCHEMA):
                wire = json.loads(json.dumps(value.to_dict()))
                _official_validator(value.SCHEMA).validate(wire)
                restored = type(value).from_dict(wire)
                self.assertEqual(restored, value)
                self.assertEqual(restored.identity, value.identity)
        self.assertEqual(self.projection.plan.trees[0].tree.aliases[0].kind, "hardlink")
        self.assertTrue(self.projection.plan.case_sensitive)

    def test_projection_refuses_mixed_fields_epoch_and_dependency_records(self):
        original = self.projection.to_dict()
        variants = [
            {**original, "unexpected": True},
            {**original, "schema": self.result.SCHEMA},
            {**original, "source_date_epoch": 946684801},
            {**original, "source_date_epoch": "946684800"},
            {**original, "maximum_archive_bytes": True},
            {**original, "dependencies": [["dependency", "devel/dependency"]]},
            {**original, "dependencies": [original["dependencies"][0]] * 2},
            {**original, "dependencies": [["dependency", "../escape", "1.0.0"]]},
            {**original, "prefix": "/usr/../escape"},
        ]
        for index, wire in enumerate(variants):
            with (
                self.subTest(index=index),
                self.assertRaises((ValueError, TypeError, PackagingError)),
            ):
                tree_pkg.FreeBsdTreePackageProjection.from_dict(wire)
        for wire in variants[:6]:
            self.assertFalse(_official_validator(self.projection.SCHEMA).is_valid(wire))

    def test_tar_and_native_result_codecs_do_not_accept_each_other(self):
        legacy = TreePackageResult(
            self.result.package_plan_identity,
            self.result.packager_identity,
            replace(self.result.archive, media_type="application/x-tar"),
        )
        with self.assertRaises(ValueError):
            TreePackageResult.from_dict(self.result.to_dict())
        with self.assertRaises(ValueError):
            tree_pkg.FreeBsdTreePackageResult.from_dict(legacy.to_dict())

    def test_raw_inventory_and_dependency_bounds_precede_nested_contract_decode(self):
        for which in ("dependencies", "members"):
            wire = copy.deepcopy(self.projection.to_dict())
            if which == "dependencies":
                wire["dependencies"] *= 1025
            else:
                files = wire["tree_package_plan"]["trees"][0]["tree"]["files"]
                files *= 100001
            with (
                self.subTest(which=which),
                mock.patch.object(
                    tree_pkg.TreePackagePlan,
                    "from_dict",
                    side_effect=AssertionError("nested allocation"),
                ),
                self.assertRaises(PackagingError),
            ):
                tree_pkg.FreeBsdTreePackageProjection.from_dict(wire)

    def test_result_refuses_other_provider_media_and_extra_fields(self):
        wire = self.result.to_dict()
        variants = [
            {**wire, "schema": self.projection.SCHEMA},
            {**wire, "extra": True},
            {**wire, "archive": {**wire["archive"], "media_type": "application/x-tar"}},
            {**wire, "archive": {**wire["archive"], "size": True}},
            {**wire, "package_plan_identity": "sha256:" + "4" * 64},
        ]
        for index, document in enumerate(variants):
            with (
                self.subTest(index=index),
                self.assertRaises((ValueError, TypeError, PackagingError)),
            ):
                tree_pkg.FreeBsdTreePackageResult.from_dict(document)
            self.assertFalse(_official_validator(self.result.SCHEMA).is_valid(document))


@unittest.skipIf(os.name == "nt", "Native tree pkg custody requires POSIX")
class FreeBsdTreePackageTests(unittest.TestCase):
    def test_composed_archive_bytes_require_real_dependency_and_provenance_payloads(
        self,
    ):
        projection, definitions = composed_fixture(
            self.plan.trees[0].tree, self.store, self.tool
        )
        results = tuple(
            archive_assertion(projection, value.identity, self.store)
            for value in projection.graph.partition.definitions
        )

        def inspect(values=results):
            return inspect_freebsd_composed_package_set(
                projection, values, store=self.store, object_root=self.work
            )

        self.assertEqual(inspect(), results)
        base = len(
            canonical_json_bytes(
                {"schema": RetainedUnitPackageResultShard.SCHEMA, "results": []}
            )
        )
        descriptor_bytes = base + max(
            len(canonical_json_bytes(value.to_dict())) for value in results
        )
        reference = retain_retained_unit_results(
            projection,
            results,
            self.store,
            max_descriptor_bytes=descriptor_bytes,
            max_descriptor_total_bytes=16384,
            max_index_bytes=4096,
            max_metadata_memory_bytes=1024**2,
        )
        index, reopened = read_retained_unit_results(projection, reference, self.store)
        self.assertEqual(len(index.shards), 2)
        self.assertEqual(inspect(reopened), results)
        returned = FileSystemCAS(self.work.parent / "returned-cas")
        transferred = (reference, *index.shards, *(value.archive for value in results))
        deadline = ActionDispatchDeadline(datetime.now(UTC) + ACTION_TEST_DEADLINE)
        # Root selection must not eagerly demand shards or archives. Complete
        # portable custody subsequently retrieves and rehashes the original set.
        index_only = FileSystemCAS(self.work.parent / "index-only")
        self.assertEqual(
            index_only.put_bytes(
                self.store.get_bytes(reference), media_type=reference.media_type
            ),
            reference,
        )
        self.assertEqual(read_retained_unit_result_index(reference, index_only), index)
        local_return = FileSystemCAS(self.work.parent / "local-return")
        local_index, local_results, local_metrics = receive_retained_unit_package_set(
            projection,
            reference,
            store=local_return,
            source=FileSystemActionBlobSource(self.store, deadline),
            object_root=self.work,
            deadline=deadline,
            admission_guard=lambda: None,
            max_descriptor_bytes=4096,
            max_descriptor_total_bytes=16384,
            max_index_bytes=4096,
            max_metadata_memory_bytes=1024**2,
        )
        self.assertEqual((local_index, local_results), (index, results))
        self.assertEqual(
            local_metrics["payload_bytes_received"],
            sum(ref.size for ref in transferred),
        )
        self.assertFalse(local_metrics["admitted"])
        self.assertFalse(local_metrics["native_verified"])
        for ref in transferred:
            self.assertEqual(local_return.get_bytes(ref), self.store.get_bytes(ref))
        source = FileSystemActionBlobSource(self.store, deadline)
        selection = RetainedUnitVerificationSelection(
            reference, source.identity, source
        )
        set_plan = projection.set_plan
        policy = RetainedUnitPackagePolicy(
            RetainedProjectLimits(500, 65536, 16 * 1024**2),
            RetainedProjectLimits(500, 65536, 16 * 1024**2),
            10,
            20,
            65536,
            set_plan.max_staging_bytes,
            set_plan.max_transport_bytes,
            set_plan.max_archive_bytes,
            set_plan.max_total_archive_bytes,
            max_descriptor_bytes=4096,
            max_descriptor_total_bytes=16384,
            max_index_bytes=4096,
            max_metadata_memory_bytes=1024**2,
        )
        selection.require_original_source(
            projection, policy, admission_guard=lambda: None
        )
        selected_archive = results[0].archive
        original = self.store.get_bytes(selected_archive)
        try:
            self.store.path_for(selected_archive).write_bytes(b"x" * len(original))
            # A valid destination cache cannot replace the selected custody.
            # This proves byte custody, never native admission.
            local_return.verify(selected_archive)
            with self.assertRaises(BlobIntegrityError):
                selection.require_original_source(
                    projection, policy, admission_guard=lambda: None
                )
        finally:
            self.store.path_for(selected_archive).write_bytes(original)
        configuration = self.work.parent / "command-return.json"
        worker = ExecutionWorker(
            "unit-return",
            ExecutionWorkerKind.COMMAND,
            command=(
                sys.executable,
                "-m",
                "literate_ai.return_cas",
                "--config",
                str(configuration),
            ),
            action_protocol=LIFECYCLE_ACTION_WIRE_PROTOCOL,
        )
        settings = {
            "schema": CONFIG_SCHEMA,
            "cas": str(self.store.root),
            "worker_identity": worker.identity.uri,
            "maximum_bytes": 16 * 1024**3,
        }
        configuration.write_bytes(canonical_json_bytes(settings))
        command_return = FileSystemCAS(self.work.parent / "command-return-cas")
        command_index, command_results, command_measurements = (
            receive_retained_unit_package_set(
                projection,
                reference,
                store=command_return,
                source=CommandActionBlobSource(
                    worker,
                    deadline,
                    registration_identity=canonical_identity(settings),
                    cwd=self.work.parent.resolve(),
                    environment=os.environ,
                ),
                object_root=self.work,
                deadline=deadline,
                admission_guard=lambda: None,
                max_descriptor_bytes=4096,
                max_descriptor_total_bytes=16384,
                max_index_bytes=4096,
                max_metadata_memory_bytes=1024**2,
            )
        )
        self.assertEqual((command_index, command_results), (index, results))
        self.assertEqual(command_measurements["blob_fetches"], len(transferred))
        self.assertEqual(
            command_measurements["payload_bytes_received"],
            sum(ref.size for ref in transferred),
        )
        self.assertFalse(command_measurements["admitted"])
        self.assertFalse(command_measurements["native_verified"])
        with source_cas_server(
            {blob_path(ref): self.store.path_for(ref) for ref in transferred}
        ) as (url, requests):
            source = HttpActionBlobSource(url, deadline, allow_http=True)

            def receive(store=returned, guard=lambda: None, ref=reference):
                return receive_retained_unit_package_set(
                    projection,
                    ref,
                    store=store,
                    source=source,
                    object_root=self.work,
                    deadline=deadline,
                    admission_guard=guard,
                    max_descriptor_bytes=4096,
                    max_descriptor_total_bytes=16384,
                    max_index_bytes=4096,
                    max_metadata_memory_bytes=1024**2,
                )

            received_index, received_results, measurements = receive()
            self.assertEqual((received_index, received_results), (index, results))
            self.assertEqual(
                measurements["payload_bytes_received"],
                sum(ref.size for ref in transferred),
            )
            self.assertEqual(measurements["blob_fetches"], len(transferred))
            self.assertFalse(measurements["admitted"])
            self.assertFalse(measurements["native_verified"])
            self.assertEqual(len(requests), len(transferred))
            self.assertEqual(receive()[2]["payload_bytes_received"], 0)
            self.assertEqual(len(requests), len(transferred))
            partial = FileSystemCAS(self.work.parent / "refused-return-cas")
            prior = partial.put_bytes(b"previous usable package")

            def revoked():
                if partial.path_for(results[0].archive).exists():
                    raise ValueError("worker custody changed")

            with self.assertRaisesRegex(ValueError, "worker custody changed"):
                receive(partial, revoked)
            partial.verify(prior)
            self.assertFalse(partial.path_for(results[1].archive).exists())
        # An overfull first descriptor exhausts the exact result count. Refuse
        # before accessing a deliberately nonexistent later descriptor.
        combined = self.store.put_manifest(
            RetainedUnitPackageResultShard(results).to_dict(),
            media_type="application/json",
        )
        missing = BlobRef("f" * 64, 1, media_type="application/json")
        forged = index.to_dict()
        forged["shards"] = [
            value.to_dict()
            for value in sorted((combined, missing), key=lambda ref: ref.digest)
        ]
        forged["max_descriptor_bytes"] = combined.size
        altered_index = self.store.put_manifest(forged, media_type="application/json")
        with self.assertRaisesRegex(ValueError, "result-actual-count-exceeds-index"):
            read_retained_unit_results(projection, altered_index, self.store)
        with source_cas_server(
            {
                blob_path(ref): self.store.path_for(ref)
                for ref in (altered_index, combined)
            }
        ) as (url, requests):
            source = HttpActionBlobSource(url, deadline, allow_http=True)
            forged_return = FileSystemCAS(self.work.parent / "forged-return-cas")
            with self.assertRaisesRegex(
                ValueError, "result-actual-count-exceeds-index"
            ):
                receive(store=forged_return, ref=altered_index)
            self.assertEqual(
                [path for path, _credential in requests],
                [blob_path(altered_index), blob_path(combined)],
            )
            returned.verify(results[0].archive)
        # These portable format assertions are not native tool execution or a
        # retained producer. The native front door must retain its platform gate.
        if platform.system().casefold() != "freebsd":
            with self.assertRaisesRegex(PackagingError, "requires FreeBSD"):
                verify_freebsd_composed_package_set(
                    projection, results, store=self.store, object_root=self.work
                )
        with self.assertRaisesRegex(PackagingError, "exactly one result"):
            inspect(
                tuple(
                    value
                    for value in results
                    if value.definition_identity != definitions["physical"].identity
                )
            )

        def flatten(entries):
            for member, body in entries:
                if member.islnk():
                    member.type, member.linkname = tarfile.REGTYPE, ""
                    body = b"compiler"
                    member.size = len(body)
                yield member, body

        def wrong_target(entries):
            for member, body in entries:
                if member.islnk():
                    member.linkname = "/foreign/cc"
                yield member, body

        def wrong_license(entries):
            for member, body in entries:
                if member.name.endswith("/license.json"):
                    body = b"x" * len(body)
                yield member, body

        for mutation, expected in (
            (flatten, "topology"),
            (wrong_target, "topology"),
            (wrong_license, "physical bytes"),
        ):
            altered = archive_assertion(
                projection, definitions["aliases"].identity, self.store, mutate=mutation
            )
            values = tuple(
                altered
                if value.definition_identity == altered.definition_identity
                else value
                for value in results
            )
            with (
                self.subTest(mutation=mutation.__name__),
                self.assertRaisesRegex(PackagingError, expected),
            ):
                inspect(values)
        altered = replace(results[0], tool_identity=canonical_identity("foreign tool"))
        with self.assertRaisesRegex(PackagingError, "another plan/graph/provider/tool"):
            inspect((altered, *results[1:]))
        small, _ = composed_fixture(
            self.plan.trees[0].tree, self.store, self.tool, record_cache_bytes=1
        )
        small_results = tuple(
            archive_assertion(small, value.identity, self.store)
            for value in small.graph.partition.definitions
        )
        with self.assertRaisesRegex(PackagingError, "verified-record cache budget"):
            inspect_freebsd_composed_package_set(
                small, small_results, store=self.store, object_root=self.work
            )
        # A malicious set cannot accumulate distinct ELF imports beyond its
        # already bounded native manifests while waiting for final comparison.
        tree = self.plan.trees[0].tree
        elf = self.store.put_bytes(freebsd_package_elf())
        changed = replace(
            tree,
            files=tuple(replace(value, blob=elf) for value in tree.files),
        )
        undeclared, _ = composed_fixture(changed, self.store, self.tool)
        elf_results = tuple(
            archive_assertion(undeclared, value.identity, self.store)
            for value in undeclared.graph.partition.definitions
        )
        with self.assertRaisesRegex(PackagingError, "undeclared shared-library"):
            inspect_freebsd_composed_package_set(
                undeclared, elf_results, store=self.store, object_root=self.work
            )
        self.assertEqual(inspect(), results)
        self.assertEqual(list(self.work.iterdir()), [])

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
        projection = tree_pkg.FreeBsdTreePackageProjection.from_dict(
            self.projection.to_dict()
        )
        adapter = tree_pkg.FreeBsdTreePackageAdapter(projection, self.tool)
        result = tree_pkg.FreeBsdTreePackageResult.from_dict(result.to_dict())
        adapter.verify(self.plan, result, store=self.store, object_root=self.work)

    def test_exact_native_tree_and_empty_directory_verify_without_native_tool(self):
        self.verify(self.archive())
        self.assertIn(
            "@dir /usr/local/lib/tree-pkg-fixture/native-sdk/empty",
            self.projection.plist.decode(),
        )
        self.assertEqual(
            self.projection.to_dict()["tree_package_plan"], self.plan.to_dict()
        )

    def test_native_selection_binds_every_source_plan_field_and_result(self):
        selected = self.adapter.selected_plan()
        self.assertEqual(selected.base.packager_identity, self.adapter.identity)
        self.assertNotEqual(selected.identity, self.plan.identity)
        self.assertEqual(
            replace(
                selected,
                base=replace(
                    selected.base, packager_identity=self.plan.base.packager_identity
                ),
            ),
            self.plan,
        )
        self.adapter._require_plan(selected, self.store)
        result = tree_pkg.FreeBsdTreePackageResult(
            selected.identity,
            self.adapter.identity,
            self.store.put_bytes(
                self.archive(), media_type="application/vnd.freebsd.pkg"
            ),
        )
        self.adapter.verify(
            selected,
            tree_pkg.FreeBsdTreePackageResult.from_dict(result.to_dict()),
            store=self.store,
            object_root=self.work,
        )
        for plan, value in (
            (self.plan, result),
            (selected, replace(result, package_plan_identity=self.plan.identity)),
        ):
            with (
                self.subTest(plan=plan.identity.uri),
                self.assertRaises(PackagingError),
            ):
                self.adapter.verify(
                    plan, value, store=self.store, object_root=self.work
                )
        for changed in (
            replace(selected, limits=replace(selected.limits, max_total_bytes=2097152)),
            replace(
                selected,
                base=replace(
                    selected.base, packager_identity=canonical_identity("other")
                ),
            ),
            replace(
                selected,
                trees=(
                    replace(
                        selected.trees[0], source_identity=canonical_identity("forged")
                    ),
                ),
            ),
        ):
            with (
                self.subTest(changed=changed.identity.uri),
                self.assertRaises(PackagingError),
            ):
                self.adapter._require_plan(changed, self.store)

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
        for value in (projection, device_projection):
            adapter = tree_pkg.FreeBsdTreePackageAdapter(value, self.tool)
            selected = adapter.selected_plan()
            self.assertEqual(
                selected.base.runtime_requirements, value.plan.base.runtime_requirements
            )
            adapter._require_plan(selected, self.store)
            stripped = replace(
                selected, base=replace(selected.base, runtime_requirements=())
            )
            with self.assertRaises(PackagingError):
                adapter._require_plan(stripped, self.store)
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
        projection = tree_pkg.FreeBsdTreePackageProjection.from_dict(
            projection.to_dict()
        )
        adapter = tree_pkg.FreeBsdTreePackageAdapter(projection, tool)
        original = adapter.package(plan, store=self.store, object_root=self.work)
        selected = adapter.selected_plan()
        first = adapter.package(selected, store=self.store, object_root=self.work)
        first = tree_pkg.FreeBsdTreePackageResult.from_dict(first.to_dict())
        second = adapter.package(selected, store=self.store, object_root=self.work)
        self.assertEqual(first, second)
        self.assertEqual(first.archive, original.archive)
        self.assertEqual(first.package_plan_identity, selected.identity)
        self.assertEqual(original.package_plan_identity, plan.identity)
        adapter.verify(selected, first, store=self.store, object_root=self.work)
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
