from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.source_materialization import (
    capture_source_archive,
)
from literate_ai.contracts import (
    ContentIdentity,
    ExecutionDispatchRequest,
    ExecutionRequirements,
    ExecutionSourceMaterializationKind,
    ExecutionWorker,
    ExecutionWorkerKind,
    HashAlgorithm,
    LifecycleDispatchAction,
)
from literate_ai.remote_source_guard import (
    SourceGuardError,
    extract_accepted_source_cache_archive,
    extract_source_archive,
    source_tree_identity,
)


def identity(character: str) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, character * 64)


def request() -> ExecutionDispatchRequest:
    worker = ExecutionWorker(
        "ssh", ExecutionWorkerKind.SSH, endpoint="u@h", workspace="~/w"
    )
    return ExecutionDispatchRequest(
        LifecycleDispatchAction.BUILD,
        "component://example/app",
        "components/app",
        "host",
        (),
        worker.identity,
        ExecutionRequirements(),
        (),
        (),
        identity("1"),
        identity("2"),
        identity("3"),
        identity("4"),
        identity("5"),
        identity("6"),
        identity("7"),
        None,
        30,
    )


class SourceMaterializationTests(unittest.TestCase):
    def test_archive_binds_request_transport_and_verified_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = root / "staging"
            project.mkdir()
            staging.mkdir()
            (project / "component.md").write_text("# app\n", encoding="utf-8")
            captured = capture_source_archive(project, request(), directory=staging)

            materialization = captured.materialization
            self.assertEqual(
                materialization.kind, ExecutionSourceMaterializationKind.ARCHIVE
            )
            self.assertEqual(materialization.request_identity, request().identity)
            self.assertEqual(
                materialization.archive_reference.identity.digest,
                hashlib.sha256(captured.path.read_bytes()).hexdigest(),
            )
            extracted = root / "extracted"
            extract_source_archive(
                captured.path, extracted, materialization.source_tree_identity.uri
            )
            self.assertEqual(
                source_tree_identity(extracted / "literate-ai"),
                materialization.source_tree_identity.uri,
            )

    def test_extraction_rejects_path_traversal_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "malicious.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                info = tarfile.TarInfo("../escape.json")
                info.size = 2
                tar.addfile(info, io.BytesIO(b"{}"))
            with self.assertRaises(SourceGuardError):
                extract_accepted_source_cache_archive(archive, root / "restored")

            symlink_archive = root / "symlink.tar.gz"
            with tarfile.open(symlink_archive, "w:gz") as tar:
                info = tarfile.TarInfo("link")
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tar.addfile(info)
            with self.assertRaises(SourceGuardError):
                extract_accepted_source_cache_archive(
                    symlink_archive, root / "restored-symlink"
                )


if __name__ == "__main__":
    unittest.main()
