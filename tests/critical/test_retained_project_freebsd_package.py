"""Retained native package custody; native construction is separately gated."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters.freebsd_packaging import FreeBsdPkgToolBinding
from literate_ai.adapters.retained_project_freebsd_package import (
    RetainedProjectFreeBsdPackage,
)
from literate_ai.application.packaging import PackagingError
from literate_ai.contracts import BlobRef, canonical_identity, canonical_json_bytes
from literate_ai.contracts.retained_project_package import (
    RetainedProjectPackagePlan,
    RetainedProjectPackageResult,
)
from literate_ai.storage.cas import BlobNotFoundError
from tests.critical import test_retained_project_package as archive
from tests.support import fixtures_test_component_lock_contracts as locked


@unittest.skipUnless(
    os.name != "nt" and shutil.which("cc"), "requires native POSIX fixture"
)
class RetainedProjectFreeBsdPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        archive.RetainedProjectPackageTests.setUpClass()
        cls.addClassCleanup(archive.RetainedProjectPackageTests.doClassCleanups)

    def setUp(self):
        self.fixture = archive.RetainedProjectPackageTests()
        self.fixture.package_provider = "pkg"
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def finish(self):
        return self.fixture.finish()

    def portable_tool(self):
        path = self.fixture.fixture.fixture.base / "unexecuted-pkg-tool"
        path.write_bytes(b"portable metadata fixture; never executed")
        path.chmod(0o755)
        return FreeBsdPkgToolBinding(
            path,
            hashlib.sha256(path.read_bytes()).hexdigest(),
            "2.8.4",
            "FreeBSD:16:amd64",
            "freebsd:16:x86:64",
        )

    def provider(self, *, tool=None, admission=None, **changes):
        return RetainedProjectFreeBsdPackage(
            self.fixture.admission if admission is None else admission,
            **{
                "provider_lock_identity": self.fixture.lock.identity,
                "provider_revision": locked.identity("flavor-pkg"),
                "tool": tool or self.portable_tool(),
                "package": "retained-fixture",
                "version": "1.0.0",
                "origin": "misc/retained-fixture",
                "target_abi": "FreeBSD:16:amd64",
                **changes,
            },
        )

    def test_projection_binds_live_receipt_both_boms_and_exact_native_provider(self):
        self.finish()
        # Only native tool probing is replaced; no construction/acceptance is claimed.
        with patch.object(FreeBsdPkgToolBinding, "require_unchanged"):
            provider = self.provider()
            provider.require_current()
        plan = provider.plan
        self.assertEqual(plan.provider, "pkg")
        self.assertEqual(plan.receipt_identity, self.fixture.admission.receipt.identity)
        self.assertEqual(RetainedProjectPackagePlan.from_dict(plan.to_dict()), plan)
        self.assertIs(plan.to_dict()["release_authorized"], False)
        expected = {
            "cyclonedx-source-sbom": self.fixture.admission.run.source_bom.content,
            "cyclonedx-resolved-sbom": self.fixture.admission.run.resolved_bom.content,
        }
        for role, content in expected.items():
            items = [item for item in plan.inputs if item.role == role]
            self.assertEqual(len(items), 1)
            self.assertEqual(self.fixture.store.get_bytes(items[0].blob), content)
        self.assertEqual(
            {v.input_path for v in provider.projection.payload},
            {v.path for v in plan.inputs},
        )
        provider.adapter._require_plan(plan)
        with self.assertRaises(PackagingError):
            provider.adapter._require_plan(replace(plan, provider="zip"))
        before = set(self.fixture.store.iter_refs())
        with patch.object(FreeBsdPkgToolBinding, "require_unchanged"):
            with self.assertRaises(BlobNotFoundError):
                provider.verify(None)
        self.assertEqual(set(self.fixture.store.iter_refs()), before)

    def test_zip_lock_and_decoded_receipt_cannot_grant_native_packaging(self):
        self.fixture.package_provider = "zip"
        self.finish()
        with self.assertRaisesRegex(ValueError, "locked-pkg-provider-required"):
            self.provider()
        with self.assertRaisesRegex(TypeError, "live-admission-required"):
            self.provider(admission=self.fixture.admission.receipt)

    def test_changed_source_and_missing_cas_payload_refuse_before_native_tool(self):
        self.finish()
        with patch.object(FreeBsdPkgToolBinding, "require_unchanged"):
            provider = self.provider()
        f = self.fixture.fixture.fixture
        path = f.source / "src/main.txt"
        original = path.read_bytes()
        path.write_bytes(b"changed original")
        with patch.object(FreeBsdPkgToolBinding, "require_unchanged") as tool:
            with self.assertRaises(ValueError):
                provider.construct(f.base)
            tool.assert_not_called()
        path.write_bytes(original)
        item = next(v for v in provider.plan.inputs if v.path == "app.bin")
        self.fixture.store.path_for(item.blob).unlink()
        with patch.object(FreeBsdPkgToolBinding, "require_unchanged") as tool:
            with self.assertRaises(BlobNotFoundError):
                provider.require_current()
            tool.assert_not_called()

    def test_incomplete_installed_projection_and_wrong_abi_refuse(self):
        self.finish()
        for change in (
            {"installed_paths": {"app.bin": "/usr/local/bin/retained-fixture"}},
            {"target_abi": "FreeBSD:15:amd64"},
        ):
            with (
                self.subTest(change=change),
                patch.object(FreeBsdPkgToolBinding, "require_unchanged"),
                self.assertRaises(PackagingError),
            ):
                self.provider(**change)

    @unittest.skipUnless(
        sys.platform.startswith("freebsd") and shutil.which("pkg-static"),
        "requires actual native FreeBSD pkg-static",
    )
    def test_native_retained_package_is_repeatable_and_reopens_exact_payload(self):
        self.finish()
        tool = FreeBsdPkgToolBinding.discover()
        provider = self.provider(tool=tool, target_abi=tool.abi)
        root = self.fixture.fixture.fixture.base
        result = provider.construct(root)
        self.assertEqual(result, provider.construct(root))
        self.assertEqual(
            RetainedProjectPackageResult.from_dict(result.to_dict()), result
        )
        provider.verify(result)
        content = self.fixture.store.get_bytes(result.artifacts[0].blob)
        projected = {
            v.input_path: v.installed_path.lstrip("/")
            for v in provider.projection.payload
        }
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:xz") as native:
            files = {m.name.lstrip("/"): m for m in native.getmembers() if m.isfile()}
            for path in ("source-bom", "resolved-bom"):
                input_path = f".literate-ai/{path}.json"
                body = native.extractfile(files[projected[input_path]]).read()
                self.assertEqual(
                    json.loads(body),
                    json.loads(
                        getattr(
                            self.fixture.admission.run, path.replace("-", "_")
                        ).content
                    ),
                )
            reopened = root / "reopened-native-app"
            reopened.write_bytes(native.extractfile(files[projected["app.bin"]]).read())
        reopened.chmod(0o755)
        observed = subprocess.run(
            (str(reopened), "echo"),
            input=b"retained native pkg\x00payload",
            capture_output=True,
            check=True,
            timeout=10,
        )
        self.assertEqual(observed.stdout, b"retained native pkg\x00payload")
        projection = provider.projection.to_dict()
        reference = BlobRef(
            canonical_identity(projection).digest, len(canonical_json_bytes(projection))
        )
        self.fixture.store.path_for(reference).unlink()
        with self.assertRaises(BlobNotFoundError):
            provider.verify(result)
