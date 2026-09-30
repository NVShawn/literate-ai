from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "docs/presentations/literate-ai-manager-overview/publish_google_workspace.py"
)
SPEC = importlib.util.spec_from_file_location("google_document_pair_publisher", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
publisher = importlib.util.module_from_spec(SPEC)
# This test intentionally loads executable documentation as a module.  Do not let
# that unusual boundary turn interpreter cache into documentation authority when the
# test is run directly rather than through the repository's profiled pytest adapter.
with patch.object(sys, "dont_write_bytecode", True):
    SPEC.loader.exec_module(publisher)


class GoogleDocumentPairPublicationTests(unittest.TestCase):
    def test_missing_and_wrong_gcloud_accounts_give_exact_recovery(self) -> None:
        missing = subprocess.CompletedProcess(("gcloud",), 0, "", "")
        with patch.object(publisher.subprocess, "run", return_value=missing):
            with self.assertRaisesRegex(RuntimeError, "gcloud auth login ACCOUNT"):
                publisher._active_account("release@example.com")

        wrong = subprocess.CompletedProcess(("gcloud",), 0, "other@example.com\n", "")
        with patch.object(publisher.subprocess, "run", return_value=wrong):
            with self.assertRaisesRegex(
                RuntimeError, "gcloud config set account release@example.com"
            ):
                publisher._active_account("release@example.com")

    def test_preflight_observes_both_stable_resources(self) -> None:
        calls: list[tuple[str, str]] = []

        def observe(_token: str, *, member: str, file_id: str, expected_mime: str):
            calls.append((member, file_id))
            return {"id": file_id, "mime_type": expected_mime}

        with patch.object(publisher, "_resource_preflight", side_effect=observe):
            result = publisher.preflight_document_pair(
                "token",
                account="release@example.com",
                slides_id="slides-id-123",
                doc_id="document-id-123",
            )
        self.assertEqual(
            calls,
            [
                ("presentation", "slides-id-123"),
                ("narrative", "document-id-123"),
            ],
        )
        self.assertTrue(result["ready"])

    def test_partial_failure_receipt_resumes_without_rewriting_first_member(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            artifact_root = repository / "docs"
            artifact_root.mkdir()
            pptx = artifact_root / "deck.pptx"
            docx = artifact_root / "narrative.docx"
            pptx.write_bytes(b"pptx")
            docx.write_bytes(b"docx")
            receipt = repository / "_build" / "publish-receipt.json"
            preflight = {"schema": publisher.PREFLIGHT_SCHEMA, "ready": True}
            first_calls: list[str] = []

            def fail_second(_token: str, file_id: str, _path: Path, _mime: str):
                first_calls.append(file_id)
                if file_id == "document-id-123":
                    raise RuntimeError("narrative update failed")
                return {"version": "2"}

            with (
                patch.object(publisher, "REPOSITORY", repository),
                patch.object(publisher, "_resumable_update", side_effect=fail_second),
                patch.object(publisher, "_ensure_org_reader", return_value=[]),
            ):
                with self.assertRaisesRegex(RuntimeError, "narrative update failed"):
                    publisher.publish_document_pair(
                        token="token",
                        preflight=preflight,
                        release_version="0.10.0",
                        source_revision="a" * 40,
                        account="release@example.com",
                        slides_id="slides-id-123",
                        doc_id="document-id-123",
                        pptx=pptx,
                        docx=docx,
                        receipt_path=receipt,
                    )
            partial = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertFalse(partial["complete"])
            self.assertTrue(partial["members"]["presentation"]["updated"])
            self.assertEqual(first_calls, ["slides-id-123", "document-id-123"])

            second_calls: list[str] = []

            def succeed(_token: str, file_id: str, _path: Path, _mime: str):
                second_calls.append(file_id)
                return {"version": "3"}

            def export(_token: str, _file_id: str, *, kind: str, destination: Path):
                destination.write_bytes(b"export-" + kind.encode())
                return destination.stat().st_size

            with (
                patch.object(publisher, "REPOSITORY", repository),
                patch.object(publisher, "_resumable_update", side_effect=succeed),
                patch.object(publisher, "_ensure_org_reader", return_value=[]),
                patch.object(publisher, "_json", return_value={"name": "edition"}),
                patch.object(publisher, "_export", side_effect=export),
                patch.object(publisher, "_pptx_pages", return_value=(26, 26)),
                patch.object(publisher, "_docx_headings", return_value=70),
            ):
                completed = publisher.publish_document_pair(
                    token="token",
                    preflight=preflight,
                    release_version="0.10.0",
                    source_revision="a" * 40,
                    account="release@example.com",
                    slides_id="slides-id-123",
                    doc_id="document-id-123",
                    pptx=pptx,
                    docx=docx,
                    receipt_path=receipt,
                )
            self.assertEqual(second_calls, ["document-id-123"])
            self.assertTrue(completed["complete"])
            without_identity = {
                key: value for key, value in completed.items() if key != "identity"
            }
            self.assertEqual(
                completed["identity"],
                publisher.canonical_identity(without_identity).uri,
            )


if __name__ == "__main__":
    unittest.main()
