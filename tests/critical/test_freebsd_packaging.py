"""Portable format refusal cases and separately gated actual FreeBSD construction."""

import copy
import hashlib
import io
import json
import lzma
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from literate_ai.adapters.freebsd_packaging import (
    FreeBsdPackageAdapter,
    FreeBsdPackageProjection,
    FreeBsdPayloadEntry,
    FreeBsdPkgToolBinding,
    project_freebsd_package,
)
from literate_ai.adapters.packaging import (
    lifecycle_archive_package_plan,
    logical_package_files,
    package_result_for,
)
from literate_ai.application.packaging import PackagingError
from literate_ai.contracts import BlobRef, canonical_json_bytes
from literate_ai.contracts.executable_components.packages import (
    PackagedFile,
    PackageFileKind,
    PackageKind,
)
from tests.support import fixtures_test_package_adapters as fixture_types
from tests.support.fixtures_test_freebsd_package_elf import freebsd_package_elf
from tests.support.fixtures_test_package_release_contracts import _identity

_EPOCH = 946684800


def _native_sum(body):
    # Independent bit-string reference for native pkg's low-bit-first base32.
    digest = hashlib.blake2b(body).digest()
    bits = "".join(f"{byte:08b}"[::-1] for byte in digest)
    alphabet = "ybndrfg8ejkmcpqxot1uwisza345h769"
    return "2$" + "".join(
        alphabet[int(bits[i : i + 5][::-1], 2)] for i in range(0, len(bits), 5)
    )


class FreeBsdPackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.fixture = fixture_types.PackageAdapterTests()
        self.fixture.setUp()
        self.contents = dict(self.fixture.contents)
        source_bom = b'{"bomFormat":"CycloneDX","specVersion":"1.7","components":[]}'
        self.resolved_bom = source_bom
        self.source_plan = lifecycle_archive_package_plan(
            self.fixture.fixture.plan(PackageKind.ARCHIVE, ()),
            packager_identity=_identity("accepted-source-packager"),
            specification=b"# Exact accepted package fixture\n",
            source_sbom=source_bom,
            source_sbom_identity=_identity("source-bom"),
            resolved_sbom=self.resolved_bom,
            resolved_sbom_source_identity=_identity("resolved-bom"),
        )
        for item in self.source_plan.inputs:
            if item.role == "component-specification":
                self.contents[item.blob.identity] = (
                    b"# Exact accepted package fixture\n"
                )
            elif item.role in {"cyclonedx-source-sbom", "cyclonedx-resolved-sbom"}:
                self.contents[item.blob.identity] = source_bom
        executable = self.root / "pkg-static"
        executable.write_bytes(b"nonexecuted portable tool binding fixture")
        executable.chmod(0o755)
        self.bind(
            FreeBsdPkgToolBinding(
                executable,
                hashlib.sha256(executable.read_bytes()).hexdigest(),
                "2.8.4",
                "FreeBSD:16:amd64",
                "freebsd:16:x86:64",
            )
        )

    def bind(self, tool):
        self.tool = tool
        self.projection = project_freebsd_package(
            self.source_plan,
            package="litai-fixture",
            version="1.0.0",
            origin="misc/litai-fixture",
            target_abi=tool.abi,
            worker_abi=tool.abi,
            worker_architecture=tool.architecture,
            resolved_sbom=self.resolved_bom,
            tool_identity=tool.identity,
        )
        self.adapter = FreeBsdPackageAdapter(self.projection, tool)
        self.plan = replace(
            self.source_plan, packager_identity=self.adapter.packager_identity
        )

    def archive(self, mutate=None):
        projected = {v.input_path: v for v in self.projection.payload}
        rows, files = [], {}
        for item in self.plan.inputs:
            entry = projected[item.path]
            body = self.contents[item.blob.identity]
            files[quote(entry.installed_path, safe="/")] = {
                "sum": _native_sum(body),
                "uname": "root",
                "gname": "wheel",
                "perm": f"{entry.mode:04o}",
                "mtime": _EPOCH,
            }
            rows.append(
                [
                    entry.installed_path[1:],
                    body,
                    entry.mode,
                    65534,
                    65534,
                    "root",
                    "wheel",
                    _EPOCH,
                ]
            )
        compact = {
            **self.projection.manifest,
            "flatsize": sum(v.blob.size for v in self.plan.inputs),
            "licenselogic": "single",
        }
        full = {**copy.deepcopy(compact), "files": files}
        if mutate:
            mutate(full, compact, rows)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:xz") as archive:
            for name, document in (("+COMPACT_MANIFEST", compact), ("+MANIFEST", full)):
                self.add(
                    archive,
                    [
                        name,
                        canonical_json_bytes(document),
                        0o644,
                        0,
                        0,
                        "root",
                        "wheel",
                        0,
                    ],
                )
            for row in rows:
                self.add(archive, row)
        content = stream.getvalue()
        blob = BlobRef(
            hashlib.sha256(content).hexdigest(),
            len(content),
            media_type="application/vnd.freebsd.pkg",
        )
        artifact = PackagedFile(
            "litai-fixture-1.0.0.pkg",
            "freebsd-pkg",
            PackageFileKind.PACKAGE_OUTPUT,
            self.plan.identity,
            self.plan.target_identity,
            blob,
            False,
        )
        result = package_result_for(
            self.plan, files=logical_package_files(self.plan), artifacts=(artifact,)
        )
        return result, content

    @staticmethod
    def add(archive, row):
        name, body, mode, uid, gid, uname, gname, mtime = row
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.uid, info.gid = len(body), mode, uid, gid
        info.uname, info.gname, info.mtime = uname, gname, mtime
        archive.addfile(info, io.BytesIO(body))

    def test_projection_roundtrip_binds_paths_abi_and_both_boms(self):
        self.assertEqual(
            FreeBsdPackageProjection.from_dict(self.projection.to_dict()),
            self.projection,
        )
        self.assertIn("@owner root\n@group wheel\n", self.projection.plist.decode())
        self.assertEqual(len(self.projection.payload), len(self.plan.inputs))
        self.assertEqual(
            self.projection.source_package_plan_identity, self.source_plan.identity
        )
        result, content = self.archive()
        self.adapter.verify_bytes(self.plan, result, content)

    def test_native_manifest_and_payload_tampering_refuse_after_outer_rehash(self):
        def owner(full, compact, rows):
            rows[0][3] = 1001

        def mode(full, compact, rows):
            rows[0][2] = 0o777

        def payload(full, compact, rows):
            rows[0][1] = b"X" * len(rows[0][1])

        def metadata(full, compact, rows):
            full["version"] = compact["version"] = "9.9.9"

        def checksum(full, compact, rows):
            first = next(iter(full["files"].values()))
            first["sum"] = "2$" + "y" * 103

        def hooks(full, compact, rows):
            full["scripts"] = {"post-install": "echo undeclared"}

        def extra(full, compact, rows):
            rows.append(
                ["usr/local/undeclared", b"extra", 0o644, 0, 0, "root", "wheel", _EPOCH]
            )

        def duplicate(full, compact, rows):
            rows.append(rows[0])

        def traversal(full, compact, rows):
            rows[0][0] = "../escape"

        for mutation in (
            owner,
            mode,
            payload,
            metadata,
            checksum,
            hooks,
            extra,
            duplicate,
            traversal,
        ):
            with self.subTest(mutation=mutation.__name__):
                result, content = self.archive(mutation)
                with self.assertRaises(PackagingError):
                    self.adapter.verify_bytes(self.plan, result, content)

    def repack_with_pax(self, headers):
        result, content = self.archive()
        output = io.BytesIO()
        with (
            tarfile.open(fileobj=io.BytesIO(content), mode="r:xz") as source,
            tarfile.open(
                fileobj=output, mode="w:xz", format=tarfile.PAX_FORMAT
            ) as target,
        ):
            for member in source:
                if not member.name.startswith("+"):
                    member.pax_headers = headers(member)
                target.addfile(member, source.extractfile(member))
        content = output.getvalue()
        artifact = result.artifacts[0]
        result = replace(
            result,
            artifacts=(
                replace(
                    artifact,
                    blob=replace(
                        artifact.blob,
                        digest=hashlib.sha256(content).hexdigest(),
                        size=len(content),
                    ),
                ),
            ),
        )
        return result, content

    def test_unplanned_extended_tar_attributes_refuse_after_outer_rehash(self):
        for key, value in (
            ("SCHILY.fflags", "uchg"),
            ("SCHILY.acl.access", "user::rwx,group::r--,other::r--"),
            ("SCHILY.xattr.user.unplanned", "unaccepted"),
            ("LIBARCHIVE.xattr.user.unplanned", "dW5hY2NlcHRlZA=="),
            ("unplanned-provider-field", "unaccepted"),
        ):
            with self.subTest(key=key):
                result, content = self.repack_with_pax(
                    lambda member, key=key, value=value: {key: value}
                )
                with self.assertRaisesRegex(PackagingError, "extended tar metadata"):
                    self.adapter.verify_bytes(self.plan, result, content)

    def test_standard_pax_fields_preserve_exact_planned_member_metadata(self):
        result, content = self.repack_with_pax(
            lambda member: {
                "path": member.name,
                "size": str(member.size),
                "mtime": str(_EPOCH),
                "uid": "65534",
                "gid": "65534",
                "uname": "root",
                "gname": "wheel",
            }
        )
        self.adapter.verify_bytes(self.plan, result, content)

    def test_native_owner_names_and_fixed_numeric_encoding_cannot_be_substituted(self):
        for index, value in ((3, 0), (4, 0), (5, ""), (6, ""), (7, 0)):
            with self.subTest(index=index, value=value):

                def substitute(full, compact, rows, index=index, value=value):
                    rows[0][index] = value

                result, content = self.archive(substitute)
                with self.assertRaisesRegex(PackagingError, "unsafe metadata/type"):
                    self.adapter.verify_bytes(self.plan, result, content)

    def test_shared_library_metadata_must_match_accepted_elf_payload(self):
        content = freebsd_package_elf(
            needed=("libexternal.so.1", "libown.so.2"), soname="libown.so.2"
        )
        reference = BlobRef(hashlib.sha256(content).hexdigest(), len(content))
        entrypoint = self.source_plan.entrypoints[0].path
        self.source_plan = replace(
            self.source_plan,
            inputs=tuple(
                replace(value, blob=reference) if value.path == entrypoint else value
                for value in self.source_plan.inputs
            ),
        )
        self.contents[reference.identity] = content
        self.bind(self.tool)

        def metadata(full, compact, rows):
            for document in (full, compact):
                document["shlibs_required"] = ["libexternal.so.1"]
                document["shlibs_provided"] = ["libown.so.2"]

        result, content = self.archive(metadata)
        self.adapter.verify_bytes(self.plan, result, content)
        for field in ("shlibs_required", "shlibs_provided"):
            for names in ([], ["libsubstituted.so.9"], ["libown.so.2"] * 2):
                with self.subTest(field=field, names=names):

                    def substitute(full, compact, rows, field=field, names=names):
                        metadata(full, compact, rows)
                        full[field] = compact[field] = names

                    result, content = self.archive(substitute)
                    with self.assertRaisesRegex(
                        PackagingError, "shared libraries differ"
                    ):
                        self.adapter.verify_bytes(self.plan, result, content)

    def test_non_elf_payload_cannot_claim_native_shared_libraries(self):
        def substitute(full, compact, rows):
            full["shlibs_required"] = compact["shlibs_required"] = ["libextra.so.1"]

        result, content = self.archive(substitute)
        with self.assertRaisesRegex(PackagingError, "shared libraries differ"):
            self.adapter.verify_bytes(self.plan, result, content)

    def test_projection_rejects_wrong_abi_missing_bom_and_path_aliases(self):
        options = dict(
            package="litai-fixture",
            version="1.0.0",
            origin="misc/litai-fixture",
            target_abi=self.tool.abi,
            worker_abi=self.tool.abi,
            worker_architecture=self.tool.architecture,
            resolved_sbom=self.resolved_bom,
            tool_identity=self.tool.identity,
        )
        with self.assertRaises(PackagingError):
            project_freebsd_package(
                self.source_plan, **{**options, "worker_abi": "FreeBSD:15:amd64"}
            )
        with self.assertRaises(PackagingError):
            project_freebsd_package(
                replace(
                    self.source_plan,
                    inputs=tuple(
                        v
                        for v in self.source_plan.inputs
                        if v.role != "cyclonedx-source-sbom"
                    ),
                ),
                **options,
            )
        for path in ("/usr/../escape", "/usr/local//file", "/usr/local/@hook\nnext"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                FreeBsdPayloadEntry("file", path, 0o644)
        first, second, *rest = self.projection.payload
        with self.assertRaises(ValueError):
            replace(
                self.projection,
                payload=(
                    first,
                    replace(second, installed_path=first.installed_path.upper()),
                    *rest,
                ),
            )

    def test_external_runtime_requires_exact_accepted_package_coordinates(self):
        requirement = self.fixture.fixture.interpreter
        plan = replace(self.source_plan, runtime_requirements=(requirement,))
        with self.assertRaisesRegex(PackagingError, "no unique accepted coordinate"):
            project_freebsd_package(
                plan,
                package="litai-fixture",
                version="1.0.0",
                origin="misc/litai-fixture",
                target_abi=self.tool.abi,
                worker_abi=self.tool.abi,
                worker_architecture=self.tool.architecture,
                resolved_sbom=self.resolved_bom,
                tool_identity=self.tool.identity,
            )
        bom = canonical_json_bytes(
            {
                "components": [
                    {
                        "properties": [
                            {
                                "name": "literate-ai:runtime-requirement-identity",
                                "value": requirement.requirement_identity.uri,
                            },
                            {
                                "name": "literate-ai:freebsd-package-name",
                                "value": "python312",
                            },
                            {
                                "name": "literate-ai:freebsd-package-origin",
                                "value": "lang/python312",
                            },
                            {
                                "name": "literate-ai:freebsd-package-version",
                                "value": "3.12.12",
                            },
                        ]
                    }
                ]
            }
        )
        reference = BlobRef(
            hashlib.sha256(bom).hexdigest(),
            len(bom),
            media_type="application/vnd.cyclonedx+json",
        )
        plan = replace(
            plan,
            inputs=tuple(
                replace(v, blob=reference) if v.role == "cyclonedx-resolved-sbom" else v
                for v in plan.inputs
            ),
        )
        projection = project_freebsd_package(
            plan,
            package="litai-fixture",
            version="1.0.0",
            origin="misc/litai-fixture",
            target_abi=self.tool.abi,
            worker_abi=self.tool.abi,
            worker_architecture=self.tool.architecture,
            resolved_sbom=bom,
            tool_identity=self.tool.identity,
        )
        self.assertEqual(
            projection.dependencies, (("python312", "lang/python312", "3.12.12"),)
        )

    def test_foreign_host_refuses_before_native_tool_execution(self):
        with (
            patch(
                "literate_ai.adapters.freebsd_packaging.platform.system",
                return_value="Linux",
            ),
            patch("literate_ai.adapters.freebsd_packaging.run_bounded_process") as run,
        ):
            with self.assertRaises(PackagingError):
                self.adapter.package(
                    self.plan, materialized_root=self.root, object_root=self.root
                )
            run.assert_not_called()

    def test_archive_limits_and_foreign_tool_binding_refuse(self):
        result, content = self.archive()
        changed = replace(self.projection, maximum_archive_bytes=1)
        adapter = FreeBsdPackageAdapter(changed, self.tool)
        changed_plan = replace(
            self.source_plan, packager_identity=adapter.packager_identity
        )
        result = package_result_for(
            changed_plan,
            files=logical_package_files(changed_plan),
            artifacts=(
                replace(result.artifacts[0], source_identity=changed_plan.identity),
            ),
        )
        with self.assertRaises(PackagingError):
            adapter.verify_bytes(changed_plan, result, content)
        with self.assertRaises(PackagingError):
            FreeBsdPackageAdapter(
                self.projection, replace(self.tool, version="different")
            )

    def test_trailing_tar_data_and_extra_xz_frames_refuse_after_outer_rehash(self):
        result, content = self.archive()
        changed = (
            content + b"trailing bytes",
            content + lzma.compress(b"extra frame"),
            lzma.compress(lzma.decompress(content) + b"X" * 512),
        )
        for body in changed:
            reference = replace(
                result.artifacts[0].blob,
                digest=hashlib.sha256(body).hexdigest(),
                size=len(body),
            )
            altered = replace(
                result, artifacts=(replace(result.artifacts[0], blob=reference),)
            )
            with self.subTest(size=len(body)), self.assertRaises(PackagingError):
                self.adapter.verify_bytes(self.plan, altered, body)

    def test_rewritten_result_cannot_redefine_original_accepted_payload(self):
        replacement = None

        def substitute(full, compact, rows):
            nonlocal replacement
            replacement = b"X" * len(rows[0][1])
            rows[0][1] = replacement
            full["files"][quote("/" + rows[0][0], safe="/")]["sum"] = _native_sum(
                replacement
            )

        result, content = self.archive(substitute)
        reference = replace(
            result.files[0].blob,
            digest=hashlib.sha256(replacement).hexdigest(),
            size=len(replacement),
        )
        forged = replace(
            result, files=(replace(result.files[0], blob=reference), *result.files[1:])
        )
        with self.assertRaisesRegex(PackagingError, "accepted package plan"):
            self.adapter.verify_bytes(self.plan, forged, content)

    def test_projection_cannot_change_accepted_file_executable_modes(self):
        changed = replace(
            self.projection,
            payload=tuple(
                replace(v, mode=0o755 if v.mode == 0o644 else 0o644)
                for v in self.projection.payload
            ),
        )
        adapter = FreeBsdPackageAdapter(changed, self.tool)
        plan = replace(self.source_plan, packager_identity=adapter.packager_identity)
        original, content = self.archive()
        result = package_result_for(
            plan,
            files=logical_package_files(plan),
            artifacts=(replace(original.artifacts[0], source_identity=plan.identity),),
        )
        with self.assertRaisesRegex(PackagingError, "accepted executable modes"):
            adapter.verify_bytes(plan, result, content)

    def test_tool_content_and_current_worker_abi_changes_refuse(self):
        self.tool.executable.write_bytes(b"changed native executable")
        with self.assertRaisesRegex(PackagingError, "executable changed"):
            self.tool.require_unchanged()
        self.tool.executable.write_bytes(b"nonexecuted portable tool binding fixture")
        with (
            patch.object(
                FreeBsdPkgToolBinding,
                "_probe",
                side_effect=("FreeBSD:15:amd64", self.tool.architecture),
            ),
            self.assertRaisesRegex(PackagingError, "worker ABI changed"),
        ):
            self.tool.require_unchanged()

    def test_optional_xz_codec_is_needed_only_when_native_inspection_runs(self):
        observed = subprocess.run(
            (
                sys.executable,
                "-c",
                "import sys; sys.modules['lzma'] = None; "
                "import literate_ai.cli.package; print('pkg-cli-import-ok')",
            ),
            check=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(observed.stdout.strip(), b"pkg-cli-import-ok")
        result, content = self.archive()
        with (
            patch.dict(sys.modules, {"lzma": None}),
            self.assertRaisesRegex(PackagingError, "requires Python's lzma codec"),
        ):
            self.adapter.verify_bytes(self.plan, result, content)


@unittest.skipUnless(
    platform.system().casefold() == "freebsd"
    and shutil.which("pkg-static")
    and shutil.which("cc"),
    "requires native FreeBSD pkg-static and C compiler",
)
class NativeFreeBsdPackageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = FreeBsdPackagingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        source, binary = f.root / "native.c", f.root / "native-app"
        source.write_text(
            '#include <stdio.h>\nint main(void) { puts("native-pkg"); return 0; }\n'
        )
        subprocess.run(
            (shutil.which("cc"), str(source), "-o", str(binary)),
            check=True,
            capture_output=True,
            timeout=30,
        )
        content = binary.read_bytes()
        reference = BlobRef(hashlib.sha256(content).hexdigest(), len(content))
        entrypoint = f.source_plan.entrypoints[0].path
        f.source_plan = replace(
            f.source_plan,
            inputs=tuple(
                replace(v, blob=reference) if v.path == entrypoint else v
                for v in f.source_plan.inputs
            ),
        )
        f.contents[reference.identity] = content
        f.bind(FreeBsdPkgToolBinding.discover())
        self.materialized = f.root / "accepted"
        self.materialized.mkdir()
        for item in f.plan.inputs:
            path = self.materialized / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f.contents[item.blob.identity])
            path.chmod(0o755 if item.executable else 0o644)

    def test_actual_pkg_is_repeatable_and_inspected_by_native_pkg_without_install(self):
        f = self.fixture
        result = f.adapter.package(
            f.plan, materialized_root=self.materialized, object_root=f.root
        )
        repeated = f.adapter.package(
            f.plan, materialized_root=self.materialized, object_root=f.root
        )
        self.assertEqual(result, repeated)
        content = f.adapter.read_created_blob(result.artifacts[0].blob)
        archive = f.root / result.artifacts[0].path
        archive.write_bytes(content)
        observed = subprocess.run(
            (*f.tool.command, "query", "-F", str(archive), "%n\t%v\t%o"),
            check=True,
            capture_output=True,
            timeout=15,
            env=f.tool.environment,
        )
        self.assertEqual(
            observed.stdout.strip(), b"litai-fixture\t1.0.0\tmisc/litai-fixture"
        )
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:xz") as packed:
            entry = next(
                v
                for v in f.projection.payload
                if v.input_path == f.plan.entrypoints[0].path
            )
            member = next(
                v for v in packed if "/" + v.name.lstrip("./") == entry.installed_path
            )
            executable = f.root / "reopened-native-app"
            executable.write_bytes(packed.extractfile(member).read())
            executable.chmod(0o755)
        actual = subprocess.run(
            (str(executable),), check=True, capture_output=True, timeout=10
        )
        self.assertEqual(actual.stdout, b"native-pkg\n")
        self.assertFalse(list(f.root.glob("freebsd-pkg-*")))

    def test_native_construction_refuses_changed_accepted_file(self):
        f = self.fixture
        (self.materialized / f.plan.entrypoints[0].path).write_bytes(b"changed")
        with self.assertRaises(PackagingError):
            f.adapter.package(
                f.plan, materialized_root=self.materialized, object_root=f.root
            )

    def test_native_package_provided_library_is_not_an_external_requirement(self):
        f = self.fixture
        library = f.root / "liblitai_fixture.so.1"
        library_source = f.root / "library.c"
        library_source.write_text("int meaning(void) { return 42; }\n")
        subprocess.run(
            (
                shutil.which("cc"),
                "-shared",
                "-fPIC",
                "-Wl,-soname,liblitai_fixture.so.1",
                str(library_source),
                "-o",
                str(library),
            ),
            check=True,
            capture_output=True,
            timeout=30,
        )
        source, binary = f.root / "library-app.c", f.root / "library-app"
        source.write_text(
            "#include <stdio.h>\nint meaning(void);\n"
            "int main(void) { if (meaning() != 42) return 1; "
            'puts("native-shlib"); return 0; }\n'
        )
        subprocess.run(
            (
                shutil.which("cc"),
                str(source),
                str(library),
                "-Wl,-rpath,$ORIGIN/../lib",
                "-o",
                str(binary),
            ),
            check=True,
            capture_output=True,
            timeout=30,
        )
        replacements = {
            f.source_plan.entrypoints[0].path: binary.read_bytes(),
            "lib/common.bin": library.read_bytes(),
        }
        inputs = []
        for item in f.source_plan.inputs:
            if item.path in replacements:
                content = replacements[item.path]
                reference = BlobRef(hashlib.sha256(content).hexdigest(), len(content))
                f.contents[reference.identity] = content
                item = replace(item, blob=reference)
            inputs.append(item)
        f.source_plan = replace(f.source_plan, inputs=tuple(inputs))
        f.bind(f.tool)
        f.projection = replace(
            f.projection,
            payload=tuple(
                sorted(
                    (
                        replace(entry, installed_path="/usr/local/lib/" + library.name)
                        if entry.input_path == "lib/common.bin"
                        else entry
                        for entry in f.projection.payload
                    ),
                    key=lambda entry: entry.installed_path,
                )
            ),
        )
        f.adapter = FreeBsdPackageAdapter(f.projection, f.tool)
        f.plan = replace(f.source_plan, packager_identity=f.adapter.packager_identity)
        for item in f.plan.inputs:
            path = self.materialized / item.path
            path.write_bytes(f.contents[item.blob.identity])
            path.chmod(0o755 if item.executable else 0o644)
        result = f.adapter.package(
            f.plan, materialized_root=self.materialized, object_root=f.root
        )
        content = f.adapter.read_created_blob(result.artifacts[0].blob)
        relocated = f.root / "reopened-library-package"
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:xz") as packed:
            manifest = json.load(packed.extractfile("+MANIFEST"))
            self.assertEqual(manifest["shlibs_required"], ["libc.so.7"])
            self.assertEqual(manifest["shlibs_provided"], [library.name])
            members = {"/" + member.name.lstrip("./"): member for member in packed}
            for entry in f.projection.payload:
                path = relocated / entry.installed_path[1:]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(
                    packed.extractfile(members[entry.installed_path]).read()
                )
                path.chmod(entry.mode)
        observed = subprocess.run(
            (str(relocated / "usr/local/bin/app"),),
            check=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(observed.stdout, b"native-shlib\n")
