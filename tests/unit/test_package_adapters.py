"""Provider-neutral package adapter and runtime-closure tests."""

from __future__ import annotations

import copy
import gzip
import hashlib
import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.conan_packaging import (
    ConanPackageAdapter,
    ConanToolBinding,
    _require_resolved_reference,
    _restored_reference,
    _verify_restored_payload,
)
from literate_ai.adapters.packaging import (
    DeterministicZipPackageAdapter,
    DirectoryPackageAdapter,
    NativeMetadataArchiveAdapter,
    NpmPackageAdapter,
    WheelPackageAdapter,
    native_metadata_archive_plan,
    npm_archive_package_plan,
)
from literate_ai.application.packaging import PackagingError, verify_package_result
from literate_ai.contracts import BlobRef
from literate_ai.contracts.executable_components.packages import (
    PackageFileKind,
    PackageInput,
    PackageKind,
    PackageResult,
    RuntimeRequirement,
    RuntimeRequirementKind,
)
from tests.unit.test_executable_component_v2_schemas import _official_validator
from tests.unit.test_package_release_contracts import (
    PackageReleaseContractTests,
    _identity,
)
from tests.unit.test_schema_catalog import SchemaCatalog


class PackageAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = PackageReleaseContractTests()
        self.fixture.setUp()
        self.contents = {
            item.blob.identity: item.export_id.encode()
            for manifest in self.fixture.graph.manifests
            for item in manifest.exports
        }

    def read(self, reference):
        return self.contents[reference.identity]

    def expanded_plan(self, package_kind, count):
        plan = self.fixture.plan(package_kind, ())
        content = b"bounded package closure\n"
        blob = BlobRef(hashlib.sha256(content).hexdigest(), len(content))
        self.contents[blob.identity] = content
        inputs = list(plan.inputs)
        for index in range(count - len(inputs)):
            inputs.append(
                PackageInput(
                    f"resources/closure-{index:05d}.txt",
                    "runtime-resource",
                    PackageFileKind.RESOURCE,
                    _identity(f"closure-{index}"),
                    plan.target_identity,
                    blob,
                )
            )
        return replace(plan, inputs=tuple(sorted(inputs, key=lambda item: item.path)))

    def native_plan(self, packager_identity):
        plan = self.fixture.plan(PackageKind.ARCHIVE, ())
        resources = []
        for path, role, content, media_type in (
            (
                "specification/component.md",
                "component-specification",
                b"# Exact sample Component\n",
                "text/markdown",
            ),
            (
                "sbom/resolved.cdx.json",
                "cyclonedx-sbom",
                b'{"bomFormat":"CycloneDX","specVersion":"1.7"}',
                "application/vnd.cyclonedx+json",
            ),
        ):
            blob = BlobRef(
                hashlib.sha256(content).hexdigest(),
                len(content),
                media_type=media_type,
            )
            self.contents[blob.identity] = content
            resources.append(
                PackageInput(
                    path,
                    role,
                    PackageFileKind.RESOURCE,
                    _identity(role),
                    plan.target_identity,
                    blob,
                )
            )
        return replace(
            plan,
            packager_identity=packager_identity,
            inputs=tuple(
                sorted((*plan.inputs, *resources), key=lambda item: item.path)
            ),
        )

    def test_standalone_directory_closure_is_truthful(self) -> None:
        plan = self.fixture.plan(PackageKind.STANDALONE_EXECUTABLE, ())
        result = DirectoryPackageAdapter().package(plan, read_blob=self.read)
        self.assertTrue(result.standalone)
        self.assertTrue(any(item.executable for item in result.artifacts))
        self.assertEqual(
            {item.source_identity for item in result.files},
            {item.source_identity for item in plan.inputs},
        )

    def test_resource_executable_mode_survives_wire_and_archive(self) -> None:
        plan = self.native_plan(_identity("mode-aware-packager"))
        resources = [
            item for item in plan.inputs if item.kind is PackageFileKind.RESOURCE
        ]
        resource = replace(resources[0], executable=True)
        self.assertEqual(PackageInput.from_dict(resource.to_dict()), resource)
        self.assertNotIn("executable", resources[0].to_dict())
        with self.assertRaises(ValueError):
            PackageInput.from_dict({**resource.to_dict(), "executable": "true"})
        plan = replace(
            plan,
            inputs=tuple(
                resource if item.path == resource.path else item for item in plan.inputs
            ),
        )
        adapter = DeterministicZipPackageAdapter()
        wire = plan.to_dict()
        SchemaCatalog().validate(wire["schema"], wire)
        result = adapter.package(plan, read_blob=self.read)
        with zipfile.ZipFile(
            io.BytesIO(adapter.read_created_blob(result.artifacts[0].blob))
        ) as archive:
            self.assertEqual(
                (archive.getinfo(resource.path).external_attr >> 16) & 0o777, 0o755
            )
            self.assertEqual(
                (archive.getinfo(resources[1].path).external_attr >> 16) & 0o777, 0o644
            )

    def test_runtime_bundle_retains_external_interpreter(self) -> None:
        requirement = RuntimeRequirement(
            "language-runtime",
            RuntimeRequirementKind.INTERPRETER,
            "language runtime",
            _identity("language-runtime"),
            False,
        )
        plan = self.fixture.plan(PackageKind.RUNTIME_BUNDLE, (requirement,))
        result = DirectoryPackageAdapter().package(plan, read_blob=self.read)
        self.assertFalse(result.standalone)
        self.assertEqual(result.runtime_requirements, (requirement,))

    def test_directory_artifact_closure_accepts_256_and_257_files(self) -> None:
        for count in (256, 257):
            with self.subTest(count=count):
                plan = self.expanded_plan(PackageKind.RUNTIME_BUNDLE, count)
                result = DirectoryPackageAdapter().package(plan, read_blob=self.read)
                self.assertEqual(len(result.artifacts), count)
                wire = result.to_dict()
                SchemaCatalog().validate(wire["schema"], wire)
                self.assertTrue(_official_validator(wire["schema"]).is_valid(wire))
                self.assertEqual(PackageResult.from_dict(wire), result)

    def test_directory_artifact_closure_accepts_large_and_bounded_maximum(self) -> None:
        representative = self.expanded_plan(PackageKind.RUNTIME_BUNDLE, 1024)
        result = DirectoryPackageAdapter().package(representative, read_blob=self.read)
        self.assertEqual(len(result.artifacts), 1024)
        with self.assertRaisesRegex(PackagingError, "digest mismatch"):
            verify_package_result(
                representative,
                result,
                read_blob=lambda reference: b"x" * reference.size,
            )

        maximum = self.expanded_plan(PackageKind.DIRECTORY, 16384)
        bounded = DirectoryPackageAdapter().package(maximum, read_blob=self.read)
        self.assertEqual(len(bounded.artifacts), 16384)
        overflow = replace(
            bounded.artifacts[-1],
            path="zz-overflow.txt",
            source_identity=_identity("overflow"),
        )
        with self.assertRaisesRegex(ValueError, "at most 16384 values"):
            replace(bounded, artifacts=(*bounded.artifacts, overflow))

    def test_outer_package_artifacts_retain_the_256_file_bound(self) -> None:
        plan = self.fixture.plan(PackageKind.ARCHIVE, ())
        adapter = DeterministicZipPackageAdapter()
        result = adapter.package(plan, read_blob=self.read)
        artifacts = tuple(
            replace(
                result.artifacts[0],
                path=f"outer/package-{index:03d}.zip",
                source_identity=_identity(f"outer-{index}"),
            )
            for index in range(257)
        )
        with self.assertRaisesRegex(ValueError, "at most 256 values"):
            replace(result, artifacts=artifacts)
        wire = result.to_dict()
        wire["artifacts"] = [item.to_dict() for item in artifacts]
        self.assertFalse(_official_validator(wire["schema"]).is_valid(wire))

    def test_archive_bytes_are_deterministic_and_cover_the_logical_tree(self) -> None:
        plan = self.fixture.plan(PackageKind.ARCHIVE, ())
        first_adapter = DeterministicZipPackageAdapter()
        second_adapter = DeterministicZipPackageAdapter()

        first = first_adapter.package(plan, read_blob=self.read)
        second = second_adapter.package(plan, read_blob=self.read)

        self.assertEqual(first, second)
        self.assertEqual(first.artifacts[0].blob, second.artifacts[0].blob)
        self.assertEqual(
            first_adapter.read_created_blob(first.artifacts[0].blob),
            second_adapter.read_created_blob(second.artifacts[0].blob),
        )
        self.assertEqual(
            {item.path for item in first.files}, set(self.fixture.destinations.values())
        )

    def test_verification_rejects_wrong_package_bytes(self) -> None:
        plan = self.fixture.plan(PackageKind.RUNTIME_BUNDLE, ())
        result = DirectoryPackageAdapter().package(plan, read_blob=self.read)
        with self.assertRaisesRegex(PackagingError, "digest mismatch"):
            verify_package_result(
                plan,
                result,
                read_blob=lambda reference: b"x" * reference.size,
            )

    def test_verification_rejects_relabelled_or_executable_logical_files(self) -> None:
        plan = self.fixture.plan(PackageKind.RUNTIME_BUNDLE, ())
        result = DirectoryPackageAdapter().package(plan, read_blob=self.read)
        original = result.files[0]
        for forged in (
            replace(original, role="forged-role"),
            replace(original, executable=not original.executable),
        ):
            with self.subTest(forged=forged):
                with self.assertRaisesRegex(PackagingError, "logical files differ"):
                    verify_package_result(
                        plan,
                        replace(result, files=(forged, *result.files[1:])),
                        read_blob=self.read,
                    )

    def test_verification_rejects_forged_outer_package_authority(self) -> None:
        plan = self.fixture.plan(PackageKind.ARCHIVE, ())
        adapter = DeterministicZipPackageAdapter()
        result = adapter.package(plan, read_blob=self.read)
        forged = replace(
            result.artifacts[0],
            source_identity=_identity("foreign-package-plan"),
        )

        with self.assertRaisesRegex(PackagingError, "exact plan and target"):
            verify_package_result(
                plan,
                replace(result, artifacts=(forged,)),
                read_blob=lambda reference: (
                    adapter.read_created_blob(reference)
                    if reference == forged.blob
                    else self.read(reference)
                ),
            )

    def test_native_wheel_is_deterministic_and_pip_installable(self) -> None:
        adapter = WheelPackageAdapter("sample_component", "1.0.0")
        plan = self.native_plan(adapter.packager_identity)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "package-root"
            for item in plan.inputs:
                path = root.joinpath(*Path(item.path).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self.read(item.blob))

            first = adapter.package(plan, materialized_root=root)
            second_adapter = WheelPackageAdapter("sample_component", "1.0.0")
            second = second_adapter.package(plan, materialized_root=root)
            self.assertEqual(first, second)
            wheel = Path(temporary) / first.artifacts[0].path
            wheel_bytes = adapter.read_created_blob(first.artifacts[0].blob)
            wheel.write_bytes(wheel_bytes)
            WheelPackageAdapter("sample_component", "1.0.0").verify_bytes(
                first, wheel.read_bytes()
            )
            installed = Path(temporary) / "installed"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-deps",
                    "--no-index",
                    "--target",
                    str(installed),
                    str(wheel),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            for item in plan.inputs:
                self.assertTrue((installed / "literate-ai" / Path(item.path)).is_file())

    def test_native_wheel_rejects_undeclared_or_changed_materialization(self) -> None:
        adapter = WheelPackageAdapter("sample_component", "1.0.0")
        plan = self.native_plan(adapter.packager_identity)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for item in plan.inputs:
                path = root.joinpath(*Path(item.path).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self.read(item.blob))
            (root / "undeclared.txt").write_text("no", encoding="utf-8")
            with self.assertRaisesRegex(PackagingError, "undeclared files"):
                adapter.package(plan, materialized_root=root)
            (root / "undeclared.txt").unlink()
            first = plan.inputs[0]
            root.joinpath(*Path(first.path).parts).write_bytes(b"changed")
            with self.assertRaisesRegex(PackagingError, "differs from plan"):
                adapter.package(plan, materialized_root=root)

    def test_native_npm_is_deterministic_and_verifies_exact_archive_closure(
        self,
    ) -> None:
        adapter = NpmPackageAdapter("sample-component", "1.0.0")
        specification = b"# Exact sample Component\n"
        source_sbom = b'{"bomFormat":"CycloneDX","lifecycle":"source"}'
        resolved_sbom = b'{"bomFormat":"CycloneDX","lifecycle":"resolved"}'
        plan = npm_archive_package_plan(
            self.fixture.plan(PackageKind.ARCHIVE, ()),
            packager_identity=adapter.packager_identity,
            specification=specification,
            source_sbom=source_sbom,
            source_sbom_identity=_identity("source-sbom"),
            resolved_sbom=resolved_sbom,
            resolved_sbom_source_identity=_identity("resolved-sbom"),
        )
        supplied = {
            **self.contents,
            next(
                item.blob.identity
                for item in plan.inputs
                if item.path == "specification/component.md"
            ): specification,
            next(
                item.blob.identity
                for item in plan.inputs
                if item.path == "sbom/source.cdx.json"
            ): source_sbom,
            next(
                item.blob.identity
                for item in plan.inputs
                if item.path == "sbom/resolved.cdx.json"
            ): resolved_sbom,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for item in plan.inputs:
                path = root.joinpath(*Path(item.path).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(supplied[item.blob.identity])
            result = adapter.package(plan, materialized_root=root)
            content = adapter.read_created_blob(result.artifacts[0].blob)
            second = NpmPackageAdapter("sample-component", "1.0.0")
            repeated = second.package(plan, materialized_root=root)
            self.assertEqual(result, repeated)
            self.assertEqual(
                content, second.read_created_blob(repeated.artifacts[0].blob)
            )
            second.verify_bytes(result, content)
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
                names = archive.getnames()
            self.assertIn("package/package.json", names)
            self.assertIn("package/literate-ai/specification/component.md", names)
            self.assertIn("package/literate-ai/sbom/source.cdx.json", names)
            self.assertIn("package/literate-ai/sbom/resolved.cdx.json", names)

            with self.assertRaisesRegex(PackagingError, "bytes differ"):
                second.verify_bytes(result, content[:-1] + bytes([content[-1] ^ 1]))
            stale = replace(result, package_plan_identity=_identity("stale-plan"))
            with self.assertRaisesRegex(PackagingError, "plan identity drifted"):
                second.verify_bytes(stale, content)

            entries: dict[str, tuple[bytes, int]] = {}
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
                for member in archive.getmembers():
                    extracted = archive.extractfile(member)
                    assert extracted is not None
                    entries[member.name] = (extracted.read(), member.mode)
            entries["package/undeclared.js"] = (b"no\n", 0o644)
            tar_stream = io.BytesIO()
            with tarfile.open(
                fileobj=tar_stream, mode="w:", format=tarfile.PAX_FORMAT
            ) as archive:
                for name in sorted(entries):
                    body, mode = entries[name]
                    member = tarfile.TarInfo(name)
                    member.size = len(body)
                    member.mode = mode
                    member.mtime = 0
                    archive.addfile(member, io.BytesIO(body))
            compressed = io.BytesIO()
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=compressed, mtime=0
            ) as stream:
                stream.write(tar_stream.getvalue())
            changed = compressed.getvalue()
            changed_blob = BlobRef(hashlib.sha256(changed).hexdigest(), len(changed))
            changed_result = replace(
                result,
                artifacts=(replace(result.artifacts[0], blob=changed_blob),),
            )
            with self.assertRaisesRegex(PackagingError, "undeclared contents"):
                second.verify_bytes(changed_result, changed)

    def test_native_conan_archive_restores_in_a_fresh_cache(self) -> None:
        conan = Path(sys.executable).parent / "conan"
        if not conan.is_file():
            self.skipTest("Conan is not installed beside the test interpreter")
        tool = ConanToolBinding.discover((str(conan),))
        adapter = ConanPackageAdapter("sample_component", "1.0.0", tool)
        plan = self.native_plan(adapter.packager_identity)
        with tempfile.TemporaryDirectory() as temporary:
            object_root = Path(temporary) / "_build"
            object_root.mkdir()
            root = Path(temporary) / "package-root"
            for item in plan.inputs:
                path = root.joinpath(*Path(item.path).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self.read(item.blob))

            result = adapter.package(
                plan,
                materialized_root=root,
                object_root=object_root,
            )
            archive = adapter.read_created_blob(result.artifacts[0].blob)
            ConanPackageAdapter("sample_component", "1.0.0", tool).verify_bytes(
                result,
                archive,
                object_root=object_root,
            )
            self.assertTrue(result.artifacts[0].path.endswith(".conan.tgz"))
            self.assertEqual(
                {item.path for item in result.files},
                {item.path for item in plan.inputs},
            )
            original = result.files[0]
            forged = replace(original, blob=BlobRef("0" * 64, original.blob.size))
            with self.assertRaisesRegex(PackagingError, "payload differs"):
                adapter.verify_bytes(
                    replace(result, files=(forged, *result.files[1:])),
                    archive,
                    object_root=object_root,
                )
            reserved = replace(plan.inputs[-1], path="conaninfo.txt")
            collision = replace(
                plan,
                inputs=tuple(
                    sorted((*plan.inputs, reserved), key=lambda item: item.path)
                ),
            )
            with self.assertRaisesRegex(PackagingError, "collides"):
                adapter.package(
                    collision, materialized_root=root, object_root=object_root
                )

    def test_conan_catalog_requires_exact_unambiguous_revisions(self) -> None:
        revision, package, binary = "a" * 32, "b" * 40, "c" * 32
        catalog = {
            "Local Cache": {
                "sample/1.0": {
                    "revisions": {
                        revision: {"packages": {package: {"revisions": {binary: {}}}}}
                    }
                }
            }
        }
        self.assertEqual(
            _restored_reference(catalog, "sample/1.0"),
            f"sample/1.0#{revision}:{package}#{binary}",
        )
        cases = [
            None,
            {},
            {"Local Cache": {"not-sample/1.0": catalog["Local Cache"]["sample/1.0"]}},
        ]
        for layer in ("recipe", "package", "binary"):
            changed = copy.deepcopy(catalog)
            recipes = changed["Local Cache"]["sample/1.0"]["revisions"]
            packages = recipes[revision]["packages"]
            binaries = packages[package]["revisions"]
            selected = {"recipe": recipes, "package": packages, "binary": binaries}[
                layer
            ]
            selected["d" * 32] = copy.deepcopy(next(iter(selected.values())))
            cases.append(changed)
        for value in cases:
            with self.subTest(catalog=value), self.assertRaises(PackagingError):
                _restored_reference(value, "sample/1.0")

    def test_conan_consumer_graph_requires_the_exact_binary_revision(self) -> None:
        reference = "sample/1.0#" + "a" * 32
        package_id = "b" * 40
        package_revision = "c" * 32
        expected = f"{reference}:{package_id}#{package_revision}"
        graph = {
            "graph": {
                "nodes": {
                    "0": {"ref": "conanfile"},
                    "1": {
                        "ref": reference,
                        "package_id": package_id,
                        "prev": package_revision,
                    },
                }
            }
        }
        _require_resolved_reference(graph, "sample/1.0", expected)
        for mutation in ("recipe", "package", "binary", "duplicate", "missing"):
            changed = copy.deepcopy(graph)
            node = changed["graph"]["nodes"]["1"]
            if mutation == "recipe":
                node["ref"] = "sample/1.0#" + "d" * 32
            elif mutation == "package":
                node["package_id"] = "d" * 40
            elif mutation == "binary":
                node["prev"] = "d" * 32
            elif mutation == "duplicate":
                changed["graph"]["nodes"]["2"] = copy.deepcopy(node)
            else:
                del changed["graph"]["nodes"]["1"]
            with self.subTest(mutation=mutation), self.assertRaises(PackagingError):
                _require_resolved_reference(changed, "sample/1.0", expected)
        for malformed in (
            None,
            {},
            {"graph": {"nodes": []}},
            {"graph": {"nodes": {"1": None}}},
            {
                "graph": {
                    "nodes": {
                        "1": {
                            "ref": "sample/1.0#not-a-revision",
                            "package_id": package_id,
                            "prev": package_revision,
                        }
                    }
                }
            },
        ):
            with self.subTest(malformed=malformed), self.assertRaises(PackagingError):
                _require_resolved_reference(malformed, "sample/1.0", expected)

    def test_conan_restored_payload_rejects_changed_missing_and_extra_files(
        self,
    ) -> None:
        plan = self.native_plan(_identity("conan-packager"))
        result = DirectoryPackageAdapter().package(
            replace(plan, package_kind=PackageKind.DIRECTORY), read_blob=self.read
        )
        for mutation in ("changed", "missing", "extra", "metadata-missing", "link"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                for item in result.files:
                    path = root / item.path
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(self.read(item.blob))
                    path.chmod(0o755 if item.executable else 0o644)
                for name in ("conaninfo.txt", "conanmanifest.txt"):
                    (root / name).write_text("native metadata\n")
                _verify_restored_payload(result, root)
                path = root / result.files[0].path
                if mutation == "changed":
                    path.write_bytes(b"substituted")
                elif mutation == "missing":
                    path.unlink()
                elif mutation == "extra":
                    (root / "undeclared.txt").write_text("extra")
                elif mutation == "metadata-missing":
                    (root / "conaninfo.txt").unlink()
                else:
                    try:
                        (root / "linked").symlink_to(path)
                    except OSError:
                        self.skipTest(
                            "This host does not permit creating symbolic links"
                        )
                with self.assertRaises(PackagingError):
                    _verify_restored_payload(result, root)

    def test_native_metadata_zip_independently_verifies_each_provider(self) -> None:
        specification = b"# Exact sample Component\n"
        resolved_sbom = b'{"bomFormat":"CycloneDX","specVersion":"1.7"}'
        for provider in ("apt", "brew", "winget", "chocolatey"):
            with self.subTest(provider=provider):
                adapter = NativeMetadataArchiveAdapter(provider, "hello", "1.0.0")
                base = replace(
                    self.fixture.plan(PackageKind.ARCHIVE, ()),
                    packager_identity=adapter.packager_identity,
                    package_kind=PackageKind.ARCHIVE,
                )
                plan, meta_path, meta_bytes = native_metadata_archive_plan(
                    base,
                    packager_identity=adapter.packager_identity,
                    specification=specification,
                    resolved_sbom=resolved_sbom,
                    resolved_sbom_source_identity=_identity("resolved-sbom"),
                    provider=provider,
                    distribution="hello",
                    version="1.0.0",
                )
                extra = {
                    meta_path: meta_bytes,
                    "specification/component.md": specification,
                    "sbom/resolved.cdx.json": resolved_sbom,
                }
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary) / "package-root"
                    for item in plan.inputs:
                        path = root.joinpath(*Path(item.path).parts)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        if item.path in extra:
                            path.write_bytes(extra[item.path])
                        else:
                            path.write_bytes(self.read(item.blob))
                    result = adapter.package(plan, materialized_root=root)
                    content = adapter.read_created_blob(result.artifacts[0].blob)
                    NativeMetadataArchiveAdapter(
                        provider, "hello", "1.0.0"
                    ).verify_bytes(result, content)
                    with self.assertRaisesRegex(
                        PackagingError, "differ from the exact result"
                    ):
                        adapter.verify_bytes(result, content + b"x")


if __name__ == "__main__":
    unittest.main()
