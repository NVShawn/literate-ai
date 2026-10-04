"""Exact bundle delivery uses explicit files or bounded authenticated TLS reads."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.directory_artifacts import directory_export_bytes
from literate_ai.adapters.retained_bundle_delivery import (
    RetainedBundleDelivery,
    RetainedBundleDeliveryError,
)
from literate_ai.contracts.blobs import BlobRef
from tests.support import fixtures_test_evidence_storage as tls_fixture


def bundle(root: Path):
    root.mkdir()
    (root / "Cargo.toml").write_text('[package]\nname = "fixture"\nversion = "1.0.0"\n')
    (root / "lib.rs").write_text("pub fn value() -> u8 { 7 }\n")
    content = directory_export_bytes(root)
    return content, BlobRef(
        hashlib.sha256(content).hexdigest(), len(content), media_type="application/zip"
    )


class RetainedBundleFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.content, self.reference = bundle(self.root / "source")
        self.path = self.root / "private-credential-canary.zip"
        self.path.write_bytes(self.content)
        self.delivery = RetainedBundleDelivery(
            offline=True, max_bytes=4096, max_entries=8
        )

    def test_offline_file_preserves_bytes_modes_and_does_not_write_or_execute(self):
        before = {
            p.relative_to(self.root): (p.stat().st_mtime_ns, p.read_bytes())
            for p in self.root.rglob("*")
            if p.is_file()
        }
        with patch(
            "http.client.HTTPSConnection", side_effect=AssertionError("network")
        ):
            files = self.delivery.read_file(self.path, self.reference)
        self.assertEqual([f.path for f in files], ["Cargo.toml", "lib.rs"])
        for file in files:
            source = self.root / "source" / file.path
            self.assertEqual(file.content, source.read_bytes())
            self.assertEqual(file.mode, source.stat().st_mode & 0o777)
        self.assertEqual(
            before,
            {
                p.relative_to(self.root): (p.stat().st_mtime_ns, p.read_bytes())
                for p in self.root.rglob("*")
                if p.is_file()
            },
        )
        with patch(
            "literate_ai.adapters.retained_bundle_delivery.HttpsEvidenceStore"
        ) as transport:
            with self.assertRaisesRegex(RetainedBundleDeliveryError, "offline"):
                self.delivery.read_https("https://example.invalid", self.reference)
            transport.assert_not_called()

    def read_with_windows_metadata(
        self, *, opened=None, final_fd=None, final_path=None
    ):
        original_lstat = Path.lstat
        actual = self.path.lstat()
        names = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
        common = {name: getattr(actual, name) for name in names}
        # Model Windows 3.12's distinct creation and metadata-change timestamps.
        pathname = {**common, "st_birthtime_ns": 100, "st_ctime_ns": 100}
        descriptor = {**common, "st_birthtime_ns": 100, "st_ctime_ns": 200}
        descriptor.update(opened or {})
        paths = iter((pathname, {**pathname, **(final_path or {})}))
        descriptors = iter((descriptor, {**descriptor, **(final_fd or {})}))

        def lstat(path, *args, **kwargs):
            if path == self.path:
                return SimpleNamespace(**next(paths))
            return original_lstat(path, *args, **kwargs)

        host = SimpleNamespace(
            **{
                **vars(os),
                "name": "nt",
                "fstat": lambda fd: SimpleNamespace(**next(descriptors)),
            }
        )
        with (
            patch("literate_ai.adapters.retained_bundle_delivery.os", host),
            patch.object(Path, "lstat", lstat),
        ):
            return self.delivery.read_file(self.path, self.reference)

    def test_windows_creation_and_change_times_can_differ_without_file_mutation(self):
        files = self.read_with_windows_metadata()
        self.assertEqual([file.path for file in files], ["Cargo.toml", "lib.rs"])
        for file in files:
            self.assertEqual(
                file.content, (self.root / "source" / file.path).read_bytes()
            )

    def test_file_with_metadata_changed_after_creation_is_readable(self):
        os.utime(self.path, ns=(1_600_000_000_000_000_000,) * 2)
        files = self.delivery.read_file(self.path, self.reference)
        for file in files:
            self.assertEqual(
                file.content, (self.root / "source" / file.path).read_bytes()
            )

    def test_windows_open_still_requires_same_file_and_creation_time(self):
        for field in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
            "st_birthtime_ns",
        ):
            with (
                self.subTest(field=field),
                patch(
                    "os.read", side_effect=AssertionError("read before identity check")
                ),
                self.assertRaisesRegex(RetainedBundleDeliveryError, "inputs-changed"),
            ):
                self.read_with_windows_metadata(opened={field: -1})

    def test_windows_metadata_change_during_read_is_still_refused(self):
        for api in ("final_fd", "final_path"):
            for field in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            ):
                with (
                    self.subTest(api=api, field=field),
                    self.assertRaisesRegex(
                        RetainedBundleDeliveryError, "inputs-changed"
                    ),
                ):
                    self.read_with_windows_metadata(**{api: {field: -1}})

    def test_size_limit_is_checked_before_open_and_corruption_is_redacted(self):
        huge = BlobRef("a" * 64, 4097, media_type="application/zip")
        with patch("os.open", side_effect=AssertionError("must not open")):
            with self.assertRaisesRegex(
                RetainedBundleDeliveryError, "reference-invalid"
            ):
                self.delivery.read_file(self.path, huge)
        for content in (
            self.content[:-1],
            b"x" * len(self.content),
            self.content + b"x",
        ):
            self.path.write_bytes(content)
            with self.assertRaises(RetainedBundleDeliveryError) as refused:
                self.delivery.read_file(self.path, self.reference)
            self.assertNotIn("private-credential-canary", str(refused.exception))
            self.assertNotIn(str(self.root), str(refused.exception))

    @unittest.skipUnless(os.name == "posix", "POSIX open-file replacement semantics")
    def test_replaced_file_is_refused_and_foreign_replacement_is_preserved(self):
        read = os.read
        replaced = False

        def swap(descriptor, count):
            nonlocal replaced
            result = read(descriptor, count)
            if not replaced:
                replaced = True
                foreign = self.root / "foreign"
                foreign.write_bytes(b"foreign state")
                os.replace(foreign, self.path)
            return result

        with patch("literate_ai.adapters.retained_bundle_delivery.os.read", swap):
            with self.assertRaisesRegex(RetainedBundleDeliveryError, "inputs-changed"):
                self.delivery.read_file(self.path, self.reference)
        self.assertEqual(self.path.read_bytes(), b"foreign state")

    def test_traversal_directory_and_entry_limits_refuse(self):
        for path in (self.root, self.root / "source" / ".." / self.path.name):
            with self.assertRaises(RetainedBundleDeliveryError):
                self.delivery.read_file(path, self.reference)
        limited = RetainedBundleDelivery(offline=True, max_bytes=4096, max_entries=1)
        with self.assertRaisesRegex(RetainedBundleDeliveryError, "archive-refused"):
            limited.read_file(self.path, self.reference)

    def test_file_alias_is_refused(self):
        alias = self.root / "alias.zip"
        try:
            alias.symlink_to(self.path)
        except (OSError, NotImplementedError):
            self.skipTest("host does not allow file symlink creation")
        with self.assertRaises(RetainedBundleDeliveryError):
            self.delivery.read_file(alias, self.reference)


class RetainedBundleHttpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tls_fixture.HttpsEvidenceStoreTests.setUpClass()
        cls.server = tls_fixture.HttpsEvidenceStoreTests.server
        cls.endpoint = tls_fixture.HttpsEvidenceStoreTests.endpoint
        cls.context = tls_fixture.HttpsEvidenceStoreTests.client_context

    @classmethod
    def tearDownClass(cls):
        tls_fixture.HttpsEvidenceStoreTests.tearDownClass()

    def setUp(self):
        tls_fixture.HttpsEvidenceStoreTests.setUp(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.content, self.reference = bundle(Path(temporary.name) / "source")
        self.location = (
            "/evidence/blobs/sha256/"
            + self.reference.digest[:2]
            + "/"
            + self.reference.digest
        )
        self.server.state["objects"][self.location] = (
            self.content,
            self.reference.media_type,
        )
        self.delivery = RetainedBundleDelivery(
            offline=False, max_bytes=4096, max_entries=8
        )

    def read(self):
        return self.delivery.read_https(
            self.endpoint,
            self.reference,
            bearer_token="credential-canary",
            tls_context=self.context,
        )

    def test_real_authenticated_tls_returns_exact_archive_without_publication(self):
        self.assertEqual([f.path for f in self.read()], ["Cargo.toml", "lib.rs"])
        requests = self.server.state["requests"]
        self.assertEqual(len(requests), 1)
        method, path, headers = requests[0]
        self.assertEqual((method, path), ("GET", self.location))
        self.assertEqual(headers["Authorization"], "Bearer credential-canary")

    def test_redirect_auth_corruption_encoding_and_truncation_do_not_fallback(self):
        for mode in (
            "redirect",
            "auth",
            "corrupt",
            "size",
            "duplicate",
            "encoding",
            "truncated",
        ):
            with self.subTest(mode=mode):
                self.server.state["mode"] = mode
                before = len(self.server.state["requests"])
                with self.assertRaisesRegex(
                    RetainedBundleDeliveryError, "https-refused"
                ) as error:
                    self.read()
                self.assertEqual(len(self.server.state["requests"]), before + 1)
                self.assertNotIn("credential-canary", str(error.exception))
                self.assertNotIn(self.endpoint, str(error.exception))
