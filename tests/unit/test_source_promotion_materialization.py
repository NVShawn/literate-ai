from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.source_to_specification.promotion_materialization import (
    GenerationInputAudit,
    GenerationInputAuditEntry,
    PromotionInputKind,
    SourcePromotionError,
    SourcePromotionInput,
    SourcePromotionMaterializer,
    _read_regular_file_windows,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


def content_identity(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


class SourcePromotionMaterializerTests(unittest.TestCase):
    def test_unlisted_source_prompt_and_journal_canaries_never_enter_closure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = root / "accepted"
            accepted.mkdir()
            specification = b"# Portable greeter\n\nPrint exactly hello.\n"
            component = b'{"schema":"component","name":"greeter"}\n'
            (accepted / "spec.md").write_bytes(specification)
            (accepted / "component.json").write_bytes(component)
            (accepted / "baseline").mkdir()
            (accepted / "baseline" / "source.txt").write_text("SOURCE-CANARY")
            (accepted / "provenance").mkdir()
            (accepted / "provenance" / "prompt.txt").write_text("PROMPT-CANARY")
            (accepted / ".literate").mkdir()
            journal = accepted / ".literate" / "source-translation.json"
            journal.write_text(
                '{"schema":"legacy","journal":"JOURNAL-CANARY"}',
                encoding="utf-8",
            )
            inputs = (
                SourcePromotionInput(
                    kind=PromotionInputKind.COMPONENT_INTENT,
                    source_root=accepted,
                    source_root_label="accepted",
                    source_path="component.json",
                    target_path="component.json",
                    expected_content_identity=content_identity(component),
                ),
                SourcePromotionInput(
                    kind=PromotionInputKind.SPECIFICATION,
                    source_root=accepted,
                    source_root_label="accepted",
                    source_path="spec.md",
                    target_path="specs/behavior.md",
                    expected_content_identity=content_identity(specification),
                ),
            )

            first = SourcePromotionMaterializer().materialize(inputs, root / "first")
            serialized = json.dumps(first.audit.to_dict(), sort_keys=True)

            self.assertEqual(
                sorted(
                    path.relative_to(first.output_root).as_posix()
                    for path in first.output_root.rglob("*")
                    if path.is_file()
                ),
                ["component.json", "specs/behavior.md"],
            )
            for canary in ("SOURCE-CANARY", "PROMPT-CANARY", "JOURNAL-CANARY"):
                self.assertNotIn(canary, serialized)
                self.assertFalse(
                    any(
                        canary.encode() in path.read_bytes()
                        for path in first.output_root.rglob("*")
                        if path.is_file()
                    )
                )
            self.assertEqual(
                GenerationInputAudit.from_dict(first.audit.to_dict()), first.audit
            )
            SchemaCatalog().validate(
                "urn:literate-ai:schema:v2:generation-input-audit",
                first.audit.to_dict(),
            )

            # Non-authority history is invisible to both closure identities.
            journal.write_text(
                '{"schema":"legacy","journal":"A-DIFFERENT-JOURNAL-CANARY"}',
                encoding="utf-8",
            )
            (accepted / "baseline" / "source.txt").write_text("OTHER-SOURCE-CANARY")
            second = SourcePromotionMaterializer().materialize(inputs, root / "second")
            self.assertEqual(first.audit.identity, second.audit.identity)
            self.assertEqual(
                first.audit.materialized_tree_identity,
                second.audit.materialized_tree_identity,
            )

            # An admitted specification edit changes both identities.
            revised = specification + b"Exit successfully.\n"
            (accepted / "spec.md").write_bytes(revised)
            revised_inputs = (
                inputs[0],
                SourcePromotionInput(
                    kind=PromotionInputKind.SPECIFICATION,
                    source_root=accepted,
                    source_root_label="accepted",
                    source_path="spec.md",
                    target_path="specs/behavior.md",
                    expected_content_identity=content_identity(revised),
                ),
            )
            third = SourcePromotionMaterializer().materialize(
                revised_inputs, root / "third"
            )
            self.assertNotEqual(first.audit.identity, third.audit.identity)
            self.assertNotEqual(
                first.audit.materialized_tree_identity,
                third.audit.materialized_tree_identity,
            )

    def test_content_pin_mismatch_fails_before_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = root / "accepted"
            accepted.mkdir()
            (accepted / "spec.md").write_text("changed")
            item = SourcePromotionInput(
                kind=PromotionInputKind.SPECIFICATION,
                source_root=accepted,
                source_root_label="accepted",
                source_path="spec.md",
                target_path="spec.md",
                expected_content_identity=content_identity(b"expected"),
            )

            with self.assertRaises(SourcePromotionError) as raised:
                SourcePromotionMaterializer().materialize((item,), root / "component")

            self.assertEqual(raised.exception.code, "promotion.input_identity_mismatch")
            self.assertFalse((root / "component").exists())

    def test_rejects_traversal_forbidden_targets_and_overlapping_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = root / "accepted"
            accepted.mkdir()
            content = b"spec"
            (accepted / "spec.md").write_bytes(content)

            with self.assertRaises(SourcePromotionError) as traversal:
                SourcePromotionInput(
                    kind=PromotionInputKind.SPECIFICATION,
                    source_root=accepted,
                    source_root_label="accepted",
                    source_path="../source.py",
                    target_path="spec.md",
                    expected_content_identity=content_identity(content),
                )
            self.assertEqual(traversal.exception.code, "promotion.invalid_path")

            with self.assertRaises(SourcePromotionError) as forbidden:
                SourcePromotionInput(
                    kind=PromotionInputKind.SPECIFICATION,
                    source_root=accepted,
                    source_root_label="accepted",
                    source_path="spec.md",
                    target_path=".literate/source-translation.json",
                    expected_content_identity=content_identity(content),
                )
            self.assertEqual(forbidden.exception.code, "promotion.forbidden_target")

            item = SourcePromotionInput(
                kind=PromotionInputKind.SPECIFICATION,
                source_root=accepted,
                source_root_label="accepted",
                source_path="spec.md",
                target_path="spec.md",
                expected_content_identity=content_identity(content),
            )
            with self.assertRaises(SourcePromotionError) as overlap:
                SourcePromotionMaterializer().materialize(
                    (item,), accepted / "component"
                )
            self.assertEqual(overlap.exception.code, "promotion.path_overlap")

    def test_persisted_audit_cannot_reintroduce_a_forbidden_target(self) -> None:
        with self.assertRaises(SourcePromotionError) as raised:
            GenerationInputAuditEntry.from_dict(
                {
                    "schema": (
                        "urn:literate-ai:schema:v2:generation-input-audit-entry"
                    ),
                    "kind": "specification",
                    "source_root_label": "accepted",
                    "source_path": "spec.md",
                    "target_path": "PROVENANCE/source-translation.json",
                    "content_identity": content_identity(b"spec"),
                    "byte_count": 4,
                }
            )
        self.assertEqual(raised.exception.code, "promotion.forbidden_target")

    def test_windows_secure_reader_declares_complete_native_signatures(self) -> None:
        import ctypes

        class NativeCall:
            def __init__(self, implementation):
                self.implementation = implementation
                self.argtypes = None
                self.restype = None

            def __call__(self, *arguments):
                return self.implementation(*arguments)

        handles = iter((101, 102, 103))
        reads = iter((b"ok", b""))

        def get_info(handle, _kind, pointer, _size):
            pointer._obj.attributes = 0x10 if handle in (101, 102) else 0
            return 1

        def get_file_information(handle, pointer):
            pointer._obj.volume_serial_number = 7
            pointer._obj.file_index_high = 0
            pointer._obj.file_index_low = handle
            pointer._obj.file_size_low = 2 if handle == 103 else 0
            pointer._obj.last_write_time.low = 11
            return 1

        def read_file(_handle, buffer, _size, read_pointer, _overlapped):
            content = next(reads)
            if content:
                ctypes.memmove(buffer, content, len(content))
            read_pointer._obj.value = len(content)
            return 1

        kernel = SimpleNamespace(
            CreateFileW=NativeCall(lambda *_arguments: next(handles)),
            CloseHandle=NativeCall(lambda _handle: 1),
            ReadFile=NativeCall(read_file),
            GetFileInformationByHandleEx=NativeCall(get_info),
            GetFileInformationByHandle=NativeCall(get_file_information),
        )
        with mock.patch.object(
            ctypes, "WinDLL", return_value=kernel, create=True
        ) as load_library:
            content = _read_regular_file_windows(
                Path("C:/project"),
                Path("folder/file.txt"),
                max_bytes=8,
            )

        self.assertEqual(content, b"ok")
        load_library.assert_called_once_with("kernel32", use_last_error=True)
        for call in (
            kernel.CreateFileW,
            kernel.CloseHandle,
            kernel.ReadFile,
            kernel.GetFileInformationByHandleEx,
            kernel.GetFileInformationByHandle,
        ):
            self.assertIsNotNone(call.argtypes)
            self.assertIsNotNone(call.restype)

    def test_windows_secure_reader_rejects_ancestor_identity_drift(self) -> None:
        import ctypes

        class NativeCall:
            def __init__(self, implementation):
                self.implementation = implementation
                self.argtypes = None
                self.restype = None

            def __call__(self, *arguments):
                return self.implementation(*arguments)

        handles = iter((101, 102, 103))
        inspections: dict[int, int] = {}

        def get_attribute_info(handle, _kind, pointer, _size):
            pointer._obj.attributes = 0x10 if handle in (101, 102) else 0
            return 1

        def get_file_information(handle, pointer):
            inspections[handle] = inspections.get(handle, 0) + 1
            pointer._obj.volume_serial_number = 7
            pointer._obj.file_index_low = (
                999 if handle == 102 and inspections[handle] > 1 else handle
            )
            return 1

        def read_file(_handle, _buffer, _size, read_pointer, _overlapped):
            read_pointer._obj.value = 0
            return 1

        kernel = SimpleNamespace(
            CreateFileW=NativeCall(lambda *_arguments: next(handles)),
            CloseHandle=NativeCall(lambda _handle: 1),
            ReadFile=NativeCall(read_file),
            GetFileInformationByHandleEx=NativeCall(get_attribute_info),
            GetFileInformationByHandle=NativeCall(get_file_information),
        )
        with mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True):
            with self.assertRaises(SourcePromotionError) as raised:
                _read_regular_file_windows(
                    Path("C:/project"),
                    Path("folder/file.txt"),
                    max_bytes=8,
                )

        self.assertEqual(raised.exception.code, "promotion.input_changed")

    def test_rejects_a_symlinked_allowlisted_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = root / "accepted"
            accepted.mkdir()
            outside = root / "outside.md"
            outside.write_bytes(b"secret")
            link = accepted / "spec.md"
            try:
                link.symlink_to(outside)
            except OSError as error:
                self.skipTest(f"host cannot create symlinks: {error}")
            item = SourcePromotionInput(
                kind=PromotionInputKind.SPECIFICATION,
                source_root=accepted,
                source_root_label="accepted",
                source_path="spec.md",
                target_path="spec.md",
                expected_content_identity=content_identity(b"secret"),
            )

            with self.assertRaises(SourcePromotionError) as raised:
                SourcePromotionMaterializer().materialize((item,), root / "component")

            self.assertEqual(raised.exception.code, "promotion.source_symlink")
            self.assertFalse((root / "component").exists())

    def test_existing_forward_audit_detects_mutation_and_parent_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            content = b'{"schema":"component"}\n'
            (project / "component.json").write_bytes(content)
            item = SourcePromotionInput(
                kind=PromotionInputKind.COMPONENT_INTENT,
                source_root=project,
                source_root_label="promoted-project",
                source_path="component.json",
                target_path="component.json",
                expected_content_identity=content_identity(content),
            )
            audit = SourcePromotionMaterializer().audit((item,))
            audit.verify(project)
            (project / "component.json").write_bytes(b"changed")
            with self.assertRaises(SourcePromotionError) as changed:
                audit.verify(project)
            self.assertEqual(changed.exception.code, "promotion.audit_target_changed")

            outside = root / "outside"
            outside.mkdir()
            (outside / "secret.json").write_bytes(content)
            linked = project / "linked"
            try:
                linked.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"host cannot create symlinks: {error}")
            linked_item = SourcePromotionInput(
                kind=PromotionInputKind.PROJECT_CONFIGURATION,
                source_root=project,
                source_root_label="promoted-project",
                source_path="linked/secret.json",
                target_path="config/secret.json",
                expected_content_identity=content_identity(content),
            )
            with self.assertRaises(SourcePromotionError) as symlink:
                SourcePromotionMaterializer().audit((linked_item,))
            self.assertEqual(symlink.exception.code, "promotion.source_symlink")


if __name__ == "__main__":
    unittest.main()
