from __future__ import annotations

import base64
import csv
import hashlib
import io
import stat
import unittest
import zipfile
import zlib
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters.dependencies.python_lock import (
    LockedPythonWheel,
    verify_locked_wheel,
)
from literate_ai.adapters.dependencies.types import DependencyObservationError
from tests.unit.test_python_wheel_lock import wheel_record

_DIST = "example-1.0.dist-info/"


class PythonWheelArchiveTests(unittest.TestCase):
    def setUp(self):
        record, content = wheel_record()
        self.package = LockedPythonWheel(**{**record, "requires_dist": ()})
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            self.files = {
                entry.filename: archive.read(entry) for entry in archive.infolist()
            }

    def record(self, algorithm="sha256"):
        content = io.StringIO(newline="")
        writer = csv.writer(content)
        for path, data in sorted(self.files.items()):
            if path == _DIST + "RECORD" or path in {
                _DIST + "RECORD.jws",
                _DIST + "RECORD.p7s",
            }:
                continue
            digest = (
                base64.urlsafe_b64encode(hashlib.new(algorithm, data).digest())
                .rstrip(b"=")
                .decode()
            )
            writer.writerow([path, algorithm + "=" + digest, len(data)])
        writer.writerow([_DIST + "RECORD", "", ""])
        self.files[_DIST + "RECORD"] = content.getvalue().encode()

    def verify(self, *, attributes=None):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, content in self.files.items():
                entry = zipfile.ZipInfo(path)
                entry.compress_type = zipfile.ZIP_DEFLATED
                if attributes and path in attributes:
                    entry.external_attr = attributes[path] << 16
                archive.writestr(entry, content)
        # Every malformed fixture has a matching outer digest. The verifier must
        # reject the inner structure, rather than merely detecting download drift.
        package = replace(
            self.package, sha256=hashlib.sha256(stream.getvalue()).hexdigest()
        )
        return verify_locked_wheel(package, stream)

    def test_payload_evidence_includes_native_bytes_and_signature_sidecars(self):
        self.files["example/native.dll"] = b"MZ\x00native fixture"
        self.files["example/native.so"] = b"\x7fELF\x00native fixture"
        self.files[_DIST + "RECORD.jws"] = b"not a verified signature"
        self.record()
        payload = self.verify()
        self.assertEqual({entry.path for entry in payload}, set(self.files))
        for entry in payload:
            self.assertEqual(
                entry.sha256, hashlib.sha256(self.files[entry.path]).hexdigest()
            )
            self.assertEqual(entry.size, len(self.files[entry.path]))

    def test_sha384_and_sha512_records(self):
        for algorithm in ("sha384", "sha512"):
            with self.subTest(algorithm=algorithm):
                self.record(algorithm)
                self.verify()

    def test_payload_drift_with_valid_outer_hash(self):
        self.files["example/__init__.py"] = b"VALUE = 2\n"
        with self.assertRaisesRegex(DependencyObservationError, "RECORD hash"):
            self.verify()

    def test_missing_and_unrecorded_files(self):
        original = dict(self.files)
        for change in ("unrecorded", "missing", "no-record", "no-wheel"):
            with self.subTest(change=change):
                self.files = dict(original)
                if change == "unrecorded":
                    self.files["extra.py"] = b"pass"
                else:
                    self.files.pop(
                        {
                            "missing": "example/__init__.py",
                            "no-record": _DIST + "RECORD",
                            "no-wheel": _DIST + "WHEEL",
                        }[change]
                    )
                with self.assertRaises(DependencyObservationError):
                    self.verify()

    def test_unsafe_member_paths_even_when_recorded(self):
        for path in (
            "../escape",
            "/absolute",
            "C:/drive",
            "pkg\\file",
            "pkg//file",
            "pkg/./file",
            "pkg/NUL.txt",
            "pkg/trailing.",
            "pkg/trailing ",
            "pkg/file:stream",
            "pkg/\x01control",
            "e\u0301/file",
        ):
            with self.subTest(path=path):
                self.setUp()
                self.files[path] = b"payload"
                self.record()
                with self.assertRaises(DependencyObservationError):
                    self.verify()

    def test_case_alias_and_file_directory_collision(self):
        for path in ("Example/other.py", "example", "EXAMPLE/__init__.py"):
            with self.subTest(path=path):
                self.setUp()
                self.files[path] = b"payload"
                self.record()
                with self.assertRaises(DependencyObservationError):
                    self.verify()

    def test_symlink_and_special_files(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFDIR):
            with self.subTest(mode=mode), self.assertRaises(DependencyObservationError):
                self.verify(attributes={"example/__init__.py": mode | 0o755})

    def test_explicit_directories_are_not_record_payload(self):
        self.files["example/"] = b""
        payload = self.verify(attributes={"example/": stat.S_IFDIR | 0o755})
        self.assertNotIn("example/", {entry.path for entry in payload})

    def test_metadata_and_data_directory_boundaries(self):
        for path in (
            "other-1.0.dist-info/entry_points.txt",
            "other-1.0.data/data/file",
            "example-1.0.data/unknown/file",
            "example-1.0.data/scripts",
        ):
            with self.subTest(path=path):
                self.setUp()
                self.files[path] = b"payload"
                self.record()
                with self.assertRaises(DependencyObservationError):
                    self.verify()

    def test_wheel_version_tags_build_and_singleton_fields(self):
        for content in (
            b"Wheel-Version: 2.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: maybe\nTag: py3-none-any\n",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: cp311-none-any\n",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\nBuild: 1\n",
            (
                b"Wheel-Version: 1.0\nWheel-Version: 1.0\n"
                b"Root-Is-Purelib: true\nTag: py3-none-any\n"
            ),
            (
                b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\n"
                b"Tag: py3-none-any\nTag: py3-none-any\n"
            ),
        ):
            with self.subTest(content=content):
                self.files[_DIST + "WHEEL"] = content
                self.record()
                with self.assertRaises(DependencyObservationError):
                    self.verify()

    def test_record_hash_format_and_size(self):
        original = self.files[_DIST + "RECORD"]
        rows = list(csv.reader(io.StringIO(original.decode())))
        for digest, size in (
            ("md5=anything", rows[0][2]),
            ("", rows[0][2]),
            (rows[0][1] + "=", rows[0][2]),
            (rows[0][1], "0"),
            (rows[0][1], ""),
        ):
            with self.subTest(digest=digest, size=size):
                changed = [list(row) for row in rows]
                changed[0][1:] = [digest, size]
                output = io.StringIO(newline="")
                csv.writer(output).writerows(changed)
                self.files[_DIST + "RECORD"] = output.getvalue().encode()
                with self.assertRaises(DependencyObservationError):
                    self.verify()

    def test_record_duplicates_invalid_csv_and_self_hash(self):
        original = self.files[_DIST + "RECORD"]
        for content in (
            original + original.splitlines(keepends=True)[0],
            b"\xff",
            b'"unterminated',
            b"too,many,columns,here\n",
            original.replace(
                (_DIST + "RECORD,,").encode(),
                (_DIST + "RECORD,sha256=invalid,0").encode(),
            ),
        ):
            with self.subTest(content=content):
                self.files[_DIST + "RECORD"] = content
                with self.assertRaises(DependencyObservationError):
                    self.verify()

    def test_archive_expansion_and_metadata_bounds(self):
        for constant in (
            "_MAX_MEMBER",
            "_MAX_EXPANDED",
            "_MAX_RECORD",
            "_MAX_WHEEL_METADATA",
        ):
            with (
                self.subTest(constant=constant),
                patch(
                    "literate_ai.adapters.dependencies.python_archive." + constant, 1
                ),
                self.assertRaises(DependencyObservationError),
            ):
                self.verify()

    def test_decompression_failure_is_a_typed_dependency_error(self):
        with patch(
            "zipfile._get_decompressor", side_effect=zlib.error("invalid stream")
        ):
            with self.assertRaises(DependencyObservationError) as caught:
                self.verify()
        self.assertEqual(caught.exception.code, "dependencies.python-wheel-invalid")


if __name__ == "__main__":
    unittest.main()
