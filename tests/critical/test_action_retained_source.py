"""Retained transfer preserves reviewed bytes without transporting host paths."""

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_retained_source import (
    RetainedGenerationInput,
    materialize_retained_input,
    transfer_retained_input,
)
from literate_ai.adapters.retained_source import (
    RetainedSourceError,
    RetainedSourceInput,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from literate_ai.contracts.generation_cache import CachedSourceFile
from literate_ai.storage import FileSystemCAS


class RetainedGenerationInputTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "main.py").write_bytes(b"print('retained')\r\n")
        (self.source / "empty.txt").write_bytes(b"")
        self.retained = RetainedSourceInput.capture(
            self.source,
            component_lock_identity=canonical_identity({"lock": 1}),
            project_authority_identity=canonical_identity({"project": 1}),
            target="host",
        )
        self.cas = FileSystemCAS(self.root / "cas")
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(minutes=2))
        self.value = RetainedGenerationInput.capture(
            self.retained, self.retained.identity.uri, self.cas, self.deadline
        )

    def admit(self, value):
        raw = canonical_json_bytes(value)
        return RetainedGenerationInput.admit(raw, record_identity(raw), self.deadline)

    def test_roundtrip_preserves_exact_review_without_host_path(self):
        raw = self.value.to_bytes()
        admitted = RetainedGenerationInput.admit(
            raw, record_identity(raw), self.deadline
        )
        self.assertEqual(admitted, self.value)
        self.assertNotIn(str(self.source).encode(), raw)
        self.assertEqual(admitted.authorization, self.retained.identity)
        self.assertEqual(
            admitted.read_files(self.cas.get_bytes, self.deadline), self.retained.files
        )

    def test_capture_requires_current_explicit_authorization(self):
        with self.assertRaises(RetainedSourceError):
            RetainedGenerationInput.capture(
                self.retained, self.retained.tree_identity.uri, self.cas, self.deadline
            )
        (self.source / "main.py").write_bytes(b"changed")
        with self.assertRaises(RetainedSourceError):
            RetainedGenerationInput.capture(
                self.retained, self.retained.identity.uri, self.cas, self.deadline
            )

    def test_recomputed_envelope_cannot_change_reviewed_authority(self):
        for key in ("target", "component_lock_identity", "project_authority_identity"):
            with self.subTest(key=key):
                value = json.loads(self.value.to_bytes())
                value["metadata"][key] = (
                    "other"
                    if key == "target"
                    else canonical_identity("other").to_dict()
                )
                with self.assertRaises(ActionWireError):
                    self.admit(value)

    def test_manifest_must_match_review_before_fetching_any_blob(self):
        for mutation in ("path", "size", "digest", "omit"):
            with self.subTest(mutation=mutation):
                value = json.loads(self.value.to_bytes())
                if mutation == "omit":
                    value["files"].pop()
                elif mutation == "path":
                    value["files"][0]["path"] = "source/another.txt"
                else:
                    value["files"][0]["blob"][mutation] = (
                        10 if mutation == "size" else "0" * 64
                    )
                with self.assertRaises(ActionWireError):
                    self.admit(value)

    def test_noncanonical_and_unknown_envelopes_refuse(self):
        for raw in (
            b"[]",
            self.value.to_bytes() + b"\n",
            b"null",
            b"{",
        ):
            with self.subTest(raw=raw[:12]), self.assertRaises(ActionWireError):
                RetainedGenerationInput.admit(raw, record_identity(raw), self.deadline)
        value = json.loads(self.value.to_bytes())
        value["local_path"] = "source"
        with self.assertRaises(ActionWireError):
            self.admit(value)

    def test_corrupt_blob_is_rejected(self):
        def corrupt(blob):
            return self.cas.get_bytes(blob) + b"x"

        with self.assertRaises(ActionWireError):
            self.value.read_files(corrupt, self.deadline)

    def test_expired_transfer_never_reads_blob(self):
        expired = ActionDispatchDeadline(datetime.now(UTC) - timedelta(seconds=1))
        reads = []
        with self.assertRaises(ActionWireError):
            self.value.read_files(lambda blob: reads.append(blob), expired)
        self.assertEqual(reads, [])

    def test_self_consistent_binary_input_is_still_refused(self):
        value = json.loads(self.value.to_bytes())
        blob = self.cas.put_bytes(b"\xff")
        value["files"] = [CachedSourceFile("source/binary.py", blob).to_dict()]
        value["metadata"]["tree_identity"] = canonical_identity(
            [{"path": "source/binary.py", "size": blob.size, "digest": blob.identity}]
        ).to_dict()
        value["authorization"] = canonical_identity(value["metadata"]).uri
        admitted = self.admit(value)
        with self.assertRaises(ActionWireError):
            admitted.read_files(self.cas.get_bytes, self.deadline)

    def test_portable_paths_and_bounds_refuse_even_with_recomputed_review(self):
        for name, size in (
            ("source/aux.py", 0),
            ("source/.codegraph/index", 0),
            ("other/main.py", 0),
            ("source/../main.py", 0),
            ("source/main.py", 8 * 1024 * 1024 + 1),
        ):
            with self.subTest(name=name, size=size):
                value = json.loads(self.value.to_bytes())
                item = value["files"][0]
                item["path"] = name
                item["blob"]["size"] = size
                value["files"] = [item]
                value["metadata"]["tree_identity"] = canonical_identity(
                    [
                        {
                            "path": name,
                            "size": size,
                            "digest": "sha256:" + item["blob"]["digest"],
                        }
                    ]
                ).to_dict()
                value["authorization"] = canonical_identity(value["metadata"]).uri
                with self.assertRaises(ActionWireError):
                    self.admit(value)

    def transfer(self, **changes):
        arguments = dict(
            cas=FileSystemCAS(self.root / "worker-cas"),
            deadline=self.deadline,
            admission_guard=lambda: None,
            blob_source=self.cas.get_bytes,
        )
        arguments.update(changes)
        return transfer_retained_input(self.value, **arguments)

    def test_transfer_to_separate_cas_verifies_all_bytes(self):
        worker = FileSystemCAS(self.root / "worker-cas")
        self.assertEqual(self.transfer(cas=worker), self.retained.files)
        for item in self.value.files:
            self.assertEqual(worker.get_bytes(item.blob), self.cas.get_bytes(item.blob))
        fallback = Mock(side_effect=AssertionError("local hit must not fetch"))
        self.assertEqual(
            self.transfer(cas=worker, blob_source=fallback), self.retained.files
        )
        fallback.assert_not_called()

    def test_corrupt_last_blob_publishes_no_fetched_input(self):
        worker = FileSystemCAS(self.root / "worker-cas")
        last = self.value.files[-1].blob

        def fetch(ref):
            return b"corrupt" if ref == last else self.cas.get_bytes(ref)

        with patch.object(worker, "put_bytes", wraps=worker.put_bytes) as publish:
            with self.assertRaises(ActionWireError):
                self.transfer(cas=worker, blob_source=fetch)
            publish.assert_not_called()

    def test_corrupt_local_blob_does_not_fall_back(self):
        worker = FileSystemCAS(self.root / "worker-cas")
        ref = self.value.files[0].blob
        worker.put_bytes(self.cas.get_bytes(ref))
        worker.path_for(ref).write_bytes(b"corrupt")
        fallback = Mock(side_effect=AssertionError("must not replace corrupt custody"))
        from literate_ai.storage import StorageError

        with self.assertRaises(StorageError):
            self.transfer(cas=worker, blob_source=fallback)
        fallback.assert_not_called()

    def test_authority_loss_during_fetch_prevents_publication(self):
        worker = FileSystemCAS(self.root / "worker-cas")
        healthy = True

        def guard():
            if not healthy:
                raise RuntimeError("authority lost")

        def fetch(ref):
            nonlocal healthy
            healthy = False
            return self.cas.get_bytes(ref)

        with patch.object(worker, "put_bytes", wraps=worker.put_bytes) as publish:
            with self.assertRaisesRegex(RuntimeError, "authority lost"):
                self.transfer(cas=worker, blob_source=fetch, admission_guard=guard)
            publish.assert_not_called()

    def test_materialized_context_preserves_foreign_replacement_on_cleanup(self):
        jobs = self.root / "jobs"
        jobs.mkdir()
        moved = self.root / "owned-moved"
        with self.assertRaises(ActionWireError):
            with materialize_retained_input(
                self.value,
                cas=self.cas,
                root=jobs,
                deadline=self.deadline,
                admission_guard=lambda: None,
            ) as retained:
                stage = retained.root.parent
                stage.rename(moved)
                stage.mkdir()
                (stage / "foreign.txt").write_bytes(b"keep")
        self.assertEqual((stage / "foreign.txt").read_bytes(), b"keep")
        self.assertTrue(moved.is_dir())

    def test_authority_loss_at_context_exit_cleans_owned_materialization(self):
        jobs = self.root / "jobs"
        jobs.mkdir()
        healthy = True

        def guard():
            if not healthy:
                raise RuntimeError("authority lost")

        with self.assertRaisesRegex(RuntimeError, "authority lost"):
            with materialize_retained_input(
                self.value,
                cas=self.cas,
                root=jobs,
                deadline=self.deadline,
                admission_guard=guard,
            ):
                healthy = False
        self.assertEqual(list(jobs.iterdir()), [])
