"""Provider-neutral package adapter and runtime-closure tests."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.conan_packaging import (
    ConanPackageAdapter,
    ConanToolBinding,
)
from literate_ai.adapters.packaging import (
    DirectoryPackageAdapter,
    WheelPackageAdapter,
)
from literate_ai.application.packaging import PackagingError, verify_package_result
from literate_ai.contracts import BlobRef
from literate_ai.contracts.executable_components.packages import (
    PackageFileKind,
    PackageInput,
    PackageKind,
)
from tests.support import fixtures_test_package_release_contracts as release_fixtures
from tests.support.fixtures_test_package_release_contracts import _identity


class PackageAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = release_fixtures.PackageReleaseContractTests()
        self.fixture.setUp()
        self.contents = {
            item.blob.identity: item.export_id.encode()
            for manifest in self.fixture.graph.manifests
            for item in manifest.exports
        }

    def read(self, reference):
        return self.contents[reference.identity]

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

    def test_verification_rejects_wrong_package_bytes(self) -> None:
        plan = self.fixture.plan(PackageKind.RUNTIME_BUNDLE, ())
        result = DirectoryPackageAdapter().package(plan, read_blob=self.read)
        with self.assertRaisesRegex(PackagingError, "digest mismatch"):
            verify_package_result(
                plan,
                result,
                read_blob=lambda reference: b"x" * reference.size,
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


if __name__ == "__main__":
    unittest.main()
