"""Package projection checks isolate verified BUILD shapes from proof admission."""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    record_identity,
)
from literate_ai.adapters.action_finalize_package import materialize_finalize_package
from literate_ai.adapters.directory_artifacts import (
    DirectoryExportFile,
    encode_directory_export,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from literate_ai.contracts.executable_components import PackageFileKind
from literate_ai.storage import FileSystemCAS


class FinalizePackageMaterializationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.cas = FileSystemCAS(self.root / "cas")
        self.workspace = self.root / "work"
        self.workspace.mkdir()
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(minutes=1))

    def archive(self, files):
        return encode_directory_export(files, max_bytes=100_000, max_entries=100)

    def materialize(self, files, content):
        archive = self.cas.put_bytes(self.archive(files))
        ref = self.cas.put_bytes(content)
        export_id = canonical_identity("export")
        linked_id = canonical_identity("link-input")
        result_raw = canonical_json_bytes({"input_identity": linked_id.uri})
        result_id = record_identity(result_raw)
        value = SimpleNamespace(
            package_input=SimpleNamespace(
                link_results=(("link", result_id),),
                plan=SimpleNamespace(
                    inputs=(
                        SimpleNamespace(
                            path="root/artifact",
                            kind=PackageFileKind.ARTIFACT,
                            source_identity=export_id,
                            blob=ref,
                        ),
                    )
                ),
            )
        )
        linked = SimpleNamespace(
            acceptance_input=SimpleNamespace(
                execution_input=SimpleNamespace(
                    build_result=SimpleNamespace(artifact_archive=archive)
                )
            ),
            manifest=SimpleNamespace(
                exports=(
                    SimpleNamespace(export_id="artifact", identity=export_id, blob=ref),
                )
            ),
        )
        self.enterContext(
            patch(
                "literate_ai.adapters.action_finalize_package.reopen_finalize_input",
                return_value=(value, object()),
            )
        )
        self.enterContext(
            patch(
                "literate_ai.adapters.action_finalize_package.LinkWorkerInput.admit",
                return_value=linked,
            )
        )
        return materialize_finalize_package(
            input_record=b"fixture",
            input_identity=canonical_identity("fixture"),
            records={result_id: result_raw, linked_id: b"fixture"},
            cas=self.cas,
            workspace_root=self.workspace,
            deadline=self.deadline,
            admission_guard=lambda: None,
            verify_package=lambda *args: None,
        )

    def test_directory_members_preserve_paths_and_intended_modes(self):
        members = (
            DirectoryExportFile("bin/tool", b"tool", 0o755),
            DirectoryExportFile("data.txt", b"data", 0o640),
        )
        files = tuple(
            DirectoryExportFile("artifact/" + item.path, item.content, item.mode)
            for item in members
        )
        with self.materialize(files, self.archive(members)) as (_, _, custody):
            self.assertTrue((custody.root / "root/artifact").is_dir())
            self.assertEqual(
                [(item.path, item.mode) for item in custody.files],
                [("root/artifact/bin/tool", 0o755), ("root/artifact/data.txt", 0o640)],
            )
            custody.require_unchanged()
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_regular_zip_file_is_not_expanded(self):
        content = self.archive((DirectoryExportFile("inside", b"payload", 0o644),))
        with self.materialize(
            (DirectoryExportFile("artifact", content, 0o644),), content
        ) as (_, _, custody):
            path = custody.root / "root/artifact"
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_mutated_staged_bytes_refuse_and_cleanup(self):
        with self.assertRaises(ValueError):
            with self.materialize(
                (DirectoryExportFile("artifact", b"original", 0o644),), b"original"
            ) as (_, _, custody):
                (custody.root / "root/artifact").write_bytes(b"changed")
        self.assertEqual(list(self.workspace.iterdir()), [])
