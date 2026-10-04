"""Native ZIP custody, reproduction and adversarial archive verification."""

from __future__ import annotations

import copy
import hashlib
import io
import tempfile
import unittest
import warnings
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters.packaging import (
    NativeZipPackageAdapter,
    lifecycle_archive_package_plan,
)
from literate_ai.application.packaging import PackagingError, verify_package_result
from literate_ai.contracts import BlobRef
from literate_ai.contracts.executable_components.packages import PackageKind
from tests.support import fixtures_test_package_release_contracts as fixtures


class NativeZipTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.PackageReleaseContractTests()
        fixture.setUp()
        self.adapter = NativeZipPackageAdapter()
        self.contents = {
            item.blob.identity: item.export_id.encode()
            for manifest in fixture.graph.manifests
            for item in manifest.exports
        }
        self.spec = b"# Portable artifact closure\n"
        self.source = b'{"bomFormat":"CycloneDX","lifecycle":"source"}'
        self.resolved = b'{"bomFormat":"CycloneDX","lifecycle":"resolved"}'
        self.plan = lifecycle_archive_package_plan(
            fixture.plan(PackageKind.DIRECTORY, ()),
            packager_identity=self.adapter.packager_identity,
            specification=self.spec,
            source_sbom=self.source,
            source_sbom_identity=fixtures._identity("source-sbom"),
            resolved_sbom=self.resolved,
            resolved_sbom_source_identity=fixtures._identity("build-evidence"),
        )
        for content in (self.spec, self.source, self.resolved):
            self.contents[
                BlobRef(hashlib.sha256(content).hexdigest(), len(content)).identity
            ] = content
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for item in self.plan.inputs:
            path = self.root / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.contents[item.blob.identity])

    def build(self):
        result = self.adapter.package(self.plan, materialized_root=self.root)
        return result, self.adapter.read_created_blob(result.artifacts[0].blob)

    def test_exact_closure_reproduces_with_independent_adapter(self):
        result, content = self.build()
        second = NativeZipPackageAdapter()
        repeated = second.package(self.plan, materialized_root=self.root)
        self.assertEqual(result, repeated)
        self.assertEqual(content, second.read_created_blob(repeated.artifacts[0].blob))
        second.verify_bytes(result, content)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            self.assertEqual(
                archive.namelist(), sorted(item.path for item in self.plan.inputs)
            )
            self.assertEqual(archive.read("sbom/source.cdx.json"), self.source)
            self.assertEqual(archive.read("sbom/resolved.cdx.json"), self.resolved)
            self.assertEqual(archive.read("specification/component.md"), self.spec)
            for entrypoint in self.plan.entrypoints:
                self.assertEqual(
                    archive.getinfo(entrypoint.path).external_attr >> 16, 0o100755
                )
        self.assertEqual(result.native_library_root, self.plan.native_library_root)
        self.assertEqual(result.native_library_layout, self.plan.native_library_layout)
        target = fixtures._identity("other-target")
        wrong_target = replace(
            self.plan,
            target_identity=target,
            inputs=tuple(
                replace(item, target_identity=target) for item in self.plan.inputs
            ),
        )
        with self.assertRaises(PackagingError):
            verify_package_result(
                wrong_target,
                result,
                read_blob=lambda reference: self.contents[reference.identity],
            )

    def test_zip64_members_and_offsets_are_verified(self):
        # Exercise large-member and large-offset records without allocating GiBs.
        with mock.patch("zipfile.ZIP64_LIMIT", 16):
            result, content = self.build()
            NativeZipPackageAdapter().verify_bytes(result, content)
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                self.assertTrue(any(member.extra for member in archive.infolist()))

    def test_changed_missing_extra_and_link_inputs_are_rejected(self):
        path = self.root / self.plan.inputs[0].path
        original = path.read_bytes()
        path.write_bytes(b"changed")
        with self.assertRaises(PackagingError):
            self.build()
        path.unlink()
        with self.assertRaises(PackagingError):
            self.build()
        path.write_bytes(original)
        extra = self.root / "undeclared"
        extra.write_bytes(b"extra")
        with self.assertRaises(PackagingError):
            self.build()
        extra.unlink()
        extra.symlink_to(path)
        with self.assertRaises(PackagingError):
            self.build()

    def test_changed_tool_and_reserved_resource_collisions_are_rejected(self):
        with self.assertRaises(PackagingError):
            self.adapter.package(
                replace(self.plan, packager_identity=fixtures._identity("other-tool")),
                materialized_root=self.root,
            )
        with self.assertRaises(PackagingError):
            lifecycle_archive_package_plan(
                self.plan,
                packager_identity=self.adapter.packager_identity,
                specification=self.spec,
                source_sbom=self.source,
                source_sbom_identity=fixtures._identity("source"),
                resolved_sbom=self.resolved,
                resolved_sbom_source_identity=fixtures._identity("resolved"),
            )

    def test_adversarial_members_fail_even_with_recomputed_outer_digest(self):
        result, content = self.build()
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            original = [(member, archive.read(member)) for member in archive.infolist()]
        for mutation in (
            "content",
            "missing",
            "extra",
            "duplicate",
            "mode",
            "timestamp",
            "order",
            "path",
            "comment",
        ):
            with self.subTest(mutation=mutation):
                entries = [(copy.copy(member), body) for member, body in original]
                member, body = entries[0]
                if mutation == "content":
                    entries[0] = (member, b"x" * len(body))
                elif mutation == "missing":
                    entries.pop()
                elif mutation in {"extra", "duplicate"}:
                    extra = copy.copy(member)
                    if mutation == "extra":
                        extra.filename = "undeclared"
                    entries.append((extra, body))
                elif mutation == "mode":
                    member.external_attr = 0o120777 << 16
                elif mutation == "timestamp":
                    member.date_time = (2026, 1, 1, 0, 0, 0)
                elif mutation == "order":
                    entries.reverse()
                elif mutation == "path":
                    member.filename = "../escape"
                elif mutation == "comment":
                    member.comment = b"extra metadata"
                stream = io.BytesIO()
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    with zipfile.ZipFile(
                        stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
                    ) as archive:
                        for member, body in entries:
                            archive.writestr(member, body)
                changed = stream.getvalue()
                artifact = replace(
                    result.artifacts[0],
                    blob=BlobRef(
                        hashlib.sha256(changed).hexdigest(),
                        len(changed),
                        media_type="application/zip",
                    ),
                )
                with self.assertRaises(PackagingError):
                    NativeZipPackageAdapter().verify_bytes(
                        replace(result, artifacts=(artifact,)), changed
                    )
        for changed in (b"prefix" + content, content + b"trailing"):
            artifact = replace(
                result.artifacts[0],
                blob=BlobRef(hashlib.sha256(changed).hexdigest(), len(changed)),
            )
            with self.assertRaises(PackagingError):
                self.adapter.verify_bytes(
                    replace(result, artifacts=(artifact,)), changed
                )
        with self.assertRaises(PackagingError):
            self.adapter.verify_bytes(result, content[:-1])
