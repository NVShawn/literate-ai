from __future__ import annotations

import base64
import csv
import hashlib
import io
import stat
import unittest
import zipfile
from dataclasses import replace

from literate_ai.adapters.dependencies.python_lock import (
    LockedPythonWheel,
    verify_locked_wheel,
)
from literate_ai.adapters.dependencies.types import DependencyObservationError
from tests.support.fixtures_test_python_wheel_lock import wheel_record

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

    def test_payload_drift_with_valid_outer_hash(self):
        self.files["example/__init__.py"] = b"VALUE = 2\n"
        with self.assertRaisesRegex(DependencyObservationError, "RECORD hash"):
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

    def test_symlink_and_special_files(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFDIR):
            with self.subTest(mode=mode), self.assertRaises(DependencyObservationError):
                self.verify(attributes={"example/__init__.py": mode | 0o755})


if __name__ == "__main__":
    unittest.main()
