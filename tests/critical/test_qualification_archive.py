"""Archive integrity is checked independently of qualification admission."""

import hashlib
import unittest

from literate_ai.adapters.directory_artifacts import (
    DirectoryExportFile,
    encode_directory_export,
)
from literate_ai.adapters.qualification_archive import (
    encode_qualification_archive,
    reopen_qualification_archive,
)
from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder
from literate_ai.contracts.blobs import BlobRef


class QualificationArchiveTests(unittest.TestCase):
    def setUp(self):
        recorder = QualificationEvidenceRecorder(max_bytes=4096, max_records=8)
        self.identity = recorder.remember_bytes(b"package bytes")
        self.entries = recorder.entries
        self.archive = encode_qualification_archive(
            self.entries, max_bytes=4096, max_records=8
        )

    def reopen(self, content, **limits):
        return reopen_qualification_archive(
            content,
            BlobRef(hashlib.sha256(content).hexdigest(), len(content)),
            **({"max_bytes": 4096, "max_records": 8} | limits),
        )

    def test_rehashed_outer_archive_cannot_hide_foreign_members(self):
        path = f"records/{self.identity.digest}"
        for name, mode, payload in (
            (path, 0o644, b"package bytes"),
            (f"other/{self.identity.digest}", 0o444, b"package bytes"),
            ("records/not-a-digest", 0o444, b"package bytes"),
            (path + "/extra", 0o444, b"package bytes"),
            (path, 0o444, b"substituted package"),
        ):
            with self.subTest(name=name, mode=mode, payload=payload):
                content = encode_directory_export(
                    (DirectoryExportFile(name, payload, mode),),
                    max_bytes=4096,
                    max_entries=8,
                )
                with self.assertRaises(ValueError):
                    self.reopen(content)
