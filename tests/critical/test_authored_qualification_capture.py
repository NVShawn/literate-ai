"""Typed Standard products and real byte transport, with injected owner admission.

Standard service composition, contracts, hashing, limits and archive reopening
are real. Original scope admission, host/model execution, oracle acceptance and
native package custody/blob I/O are explicit fixtures. No genuine qualification,
native retained consumption or positive publication is established here.
"""

import hashlib
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.authored_qualification_capture import (
    capture_authored_qualification_products,
    reopen_authored_product_membership,
)
from literate_ai.adapters.authored_qualification_execution import (
    CurrentAuthoredQualificationExecution,
)
from literate_ai.adapters.qualification_archive import (
    encode_qualification_archive,
    reopen_qualification_archive,
)
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
)
from literate_ai.contracts.authored_qualification_products import (
    AuthoredQualificationProducts,
)
from literate_ai.contracts.authored_qualification_runs import (
    AuthoredQualificationResult,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from tests.critical import test_authored_qualification_runs as runs
from tests.support.fixtures_test_library_products import library_product
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


class AuthoredQualificationCaptureTests(unittest.TestCase):
    def setUp(self):
        class LibraryRuns(runs.AuthoredQualificationRunTests):
            def run_fixture(this, label):
                this.library_exports = True
                if not hasattr(this, "selections"):
                    this.selections, this.run_identities, this.allocation_identities = (
                        {},
                        {},
                        {},
                    )
                selected = {
                    "schema": "literate-ai/authored-qualification-run-selection@1",
                    "plan_identity": this.plan.identity.uri,
                    "session_identity": canonical_identity(
                        {"fixture-session": True}
                    ).uri,
                    "ordinal": len(this.selections),
                }
                this.selections[label] = selected
                this.run_identities[label] = canonical_identity(selected)
                this.allocation_identities[label] = canonical_identity(
                    {
                        "schema": "literate-ai/qualification-workspace-allocation@1",
                        "session_identity": selected["session_identity"],
                        "run_identity": this.run_identities[label].uri,
                    }
                )
                return super().run_fixture(label)

        f = LibraryRuns("runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.fixture = f
        self.result = AuthoredQualificationResult(f.plan, f.runs)
        self.recorder = QualificationEvidenceRecorder(
            max_bytes=8_000_000, max_records=2000
        )
        for selected in f.selections.values():
            self.recorder.remember_json(selected)
        self.active, self.reads, self.payloads = True, [], {}
        surface = library_product("rust").import_surface
        lock = f.fixture.lock
        bindings = tuple(
            SimpleNamespace(
                component_revision_identity=n.revision.identity,
                oracle=SimpleNamespace(import_surface_identity=surface.identity),
            )
            for n in lock.nodes
        )
        current = SimpleNamespace(
            plan=f.plan,
            oracles=SimpleNamespace(
                authority=SimpleNamespace(
                    locked=SimpleNamespace(authority=SimpleNamespace(lock=lock))
                ),
                bindings=bindings,
            ),
        )
        sources = {}
        for label, port in f.ports.items():
            execution = f.executions[label]
            sources[
                next(
                    r.request.run_identity.uri
                    for r in f.runs
                    if r.lifecycle_result_identity == execution.lifecycle.identity
                )
            ] = (execution, port)
            port.contracts = {
                n.revision.identity.uri: SimpleNamespace(
                    library_import_surface=surface, native_layout=None
                )
                for n in lock.nodes
            }
            port.project_package_custody = Mock()
            for node in execution.lifecycle.node_results:
                for export in node.exports:
                    self.payloads[export.blob.identity] = f.fixture.names[
                        node.component_revision.uri
                    ].encode()

            def read(reference, selected_label=label):
                self.reads.append((selected_label, reference.identity))
                return self.payloads[reference.identity]

            port.read_artifact_blob = read
        self.lifecycle = SimpleNamespace(authored_plan=current, product_sources=sources)

        def check():
            if not self.active:
                raise ValueError("fixture original owner expired")

        self.owner = CurrentAuthoredQualificationExecution(
            self.result, self.lifecycle, self.recorder, check
        )

    def reader(self, entries):
        return QualificationEvidenceReader(
            entries, max_bytes=8_000_000, max_records=2000
        )

    def test_every_run_and_library_is_captured_with_shared_bytes_stored_once(self):
        capture = capture_authored_qualification_products(self.owner)
        self.assertEqual(len(capture.products.runs), 2)
        self.assertEqual(len(self.reads), 4)
        self.assertEqual(len({identity for _label, identity in self.reads}), 4)
        self.assertEqual([r.generated_test_total for r in self.result.runs], [4, 4])
        self.assertEqual(
            AuthoredQualificationProducts.from_dict(capture.products.to_dict()),
            capture.products,
        )
        SchemaCatalog().validate(capture.products.SCHEMA, capture.products.to_dict())
        reopened = reopen_authored_product_membership(
            self.reader(capture.records), capture.products.identity
        )
        self.assertEqual(reopened.result, self.result)
        self.assertEqual(len(reopened.exports), 2)
        self.assertEqual([len(e.libraries) for _r, e in reopened.exports], [4, 4])

    def test_expired_or_passive_owner_cannot_read_physical_products(self):
        self.active = False
        with self.assertRaisesRegex(ValueError, "expired"):
            capture_authored_qualification_products(self.owner)
        with self.assertRaises(TypeError):
            capture_authored_qualification_products(self.result)
        self.assertEqual(self.reads, [])

    def test_missing_or_substituted_run_refuses_before_artifact_read(self):
        key = next(iter(self.lifecycle.product_sources))
        self.lifecycle.product_sources.pop(key)
        with self.assertRaisesRegex(QualificationCaptureError, "capture_run_mismatch"):
            capture_authored_qualification_products(self.owner)
        self.assertEqual(self.reads, [])

    def test_global_budget_includes_index_and_shared_records_before_blob_reads(self):
        captured = capture_authored_qualification_products(self.owner)
        blob_ids = {ContentIdentity.parse_uri(i) for i in self.payloads}
        total = sum(len(b) for _i, b in captured.records)
        recorder = QualificationEvidenceRecorder(max_bytes=total - 1, max_records=2000)
        for identity, payload in captured.records:
            if identity not in blob_ids and identity != captured.products.identity:
                recorder.remember_bytes(payload)
        self.reads.clear()
        with self.assertRaisesRegex(QualificationCaptureError, "byte-limit"):
            capture_authored_qualification_products(
                replace(self.owner, _recorder=recorder)
            )
        self.assertEqual(self.reads, [])

    def test_corrupt_artifact_and_rehashed_cross_run_substitution_refuse(self):
        capture = capture_authored_qualification_products(self.owner)
        members = capture.products.runs
        wrong = replace(
            capture.products,
            runs=(
                replace(members[0], run_record_identity=members[1].run_record_identity),
                members[1],
            ),
        )
        wrong_identity = self.recorder.remember_json(wrong.to_dict())
        with self.assertRaisesRegex(QualificationCaptureError, "capture_run_mismatch"):
            reopen_authored_product_membership(
                self.reader(self.recorder.entries), wrong_identity
            )
        blob_ids = {ContentIdentity.parse_uri(i) for i in self.payloads}
        entries = tuple((i, b) for i, b in capture.records if i not in blob_ids)
        with self.assertRaisesRegex(QualificationCaptureError, "record-missing"):
            reopen_authored_product_membership(
                self.reader(entries), capture.products.identity
            )

    def test_archive_reopens_after_discarding_fixture_state_without_execution(
        self,
    ):
        capture = capture_authored_qualification_products(self.owner)
        expected = self.result.to_dict()
        archive = encode_qualification_archive(
            capture.records, max_bytes=8_000_000, max_records=2000
        )
        reference = BlobRef(
            hashlib.sha256(archive).hexdigest(),
            len(archive),
            media_type="application/zip",
        )
        self.active = False
        self.lifecycle.product_sources.clear()
        self.payloads.clear()
        with patch(
            "subprocess.run",
            side_effect=AssertionError("archive reopening executed content"),
        ):
            transported = reopen_qualification_archive(
                archive, reference, max_bytes=8_000_000, max_records=2000
            )
            reopened = reopen_authored_product_membership(
                transported, capture.products.identity
            )
        self.assertEqual(reopened.result.to_dict(), expected)

    def test_source_history_and_unknown_origin_are_not_product_fields(self):
        capture = capture_authored_qualification_products(self.owner)
        for changed in (
            {
                **capture.products.to_dict(),
                "promotion_tree_identity": self.result.identity.to_dict(),
            },
            {**capture.products.to_dict(), "origin": {"origin": "source-derived"}},
        ):
            with self.assertRaises(ValueError):
                AuthoredQualificationProducts.from_dict(changed)
            with self.assertRaises(AssertionError):
                SchemaCatalog().validate(capture.products.SCHEMA, changed)

    def test_changed_bytes_and_owner_expiry_during_read_return_no_capture(self):
        identity = next(iter(self.payloads))
        self.payloads[identity] = b"substituted native bytes"
        with self.assertRaisesRegex(QualificationCaptureError, "blob-mismatch"):
            capture_authored_qualification_products(self.owner)
        self.assertFalse(
            any(
                json.loads(b).get("schema") == AuthoredQualificationProducts.SCHEMA
                for _i, b in self.recorder.entries
                if b.startswith(b"{")
            )
        )

    def test_owner_expiry_after_blob_read_cannot_publish_product_index(self):
        for port in self.fixture.ports.values():
            original = port.read_artifact_blob

            def expire(reference, read=original):
                payload = read(reference)
                # Repeated content is read once across both runs. Expiry at the
                # final unique payload must refuse before recording the index.
                if len(self.reads) == len(self.payloads):
                    self.active = False
                return payload

            port.read_artifact_blob = expire
        with self.assertRaisesRegex(ValueError, "expired"):
            capture_authored_qualification_products(self.owner)
        self.assertTrue(self.reads)
        self.assertFalse(
            any(
                json.loads(b).get("schema") == AuthoredQualificationProducts.SCHEMA
                for _i, b in self.recorder.entries
                if b.startswith(b"{")
            )
        )

    def test_missing_authoring_and_rehashed_bad_run_selection_refuse(self):
        capture = capture_authored_qualification_products(self.owner)
        authoring = self.fixture.fixture.lock.nodes[0].revision.authoring_identity
        missing = tuple((i, b) for i, b in capture.records if i != authoring)
        with self.assertRaisesRegex(QualificationCaptureError, "capture_lock_mismatch"):
            reopen_authored_product_membership(
                self.reader(missing), capture.products.identity
            )
        first = self.result.runs[0]
        selection = {**self.fixture.selections["one"], "ordinal": True}
        new_id = self.recorder.remember_json(selection)
        allocation = canonical_identity(
            {
                "schema": "literate-ai/qualification-workspace-allocation@1",
                "session_identity": selection["session_identity"],
                "run_identity": new_id.uri,
            }
        )
        changed = replace(
            first,
            request=replace(
                first.request,
                run_identity=new_id,
                workspace_allocation_identity=allocation,
            ),
            comparison=replace(first.comparison, run_identity=new_id),
        )
        result = replace(self.result, runs=(changed, *self.result.runs[1:]))
        self.recorder.remember_json(changed.to_dict())
        self.recorder.remember_json(result.to_dict())
        members = tuple(
            replace(m, run_identity=new_id, run_record_identity=changed.identity)
            if m.run_identity == first.request.run_identity
            else m
            for m in capture.products.runs
        )
        products = replace(
            capture.products,
            qualification_identity=result.identity,
            runs=tuple(sorted(members, key=lambda m: m.run_identity.uri)),
        )
        identity = self.recorder.remember_json(products.to_dict())
        with self.assertRaisesRegex(
            QualificationCaptureError, "capture_selection_mismatch"
        ):
            reopen_authored_product_membership(
                self.reader(self.recorder.entries), identity
            )
