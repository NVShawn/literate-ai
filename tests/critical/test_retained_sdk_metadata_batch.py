"""Real immutable CAS contracts with fake boundary guards; no SDK process runs."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.retained_project_sdk_runtime import RetainedProjectSdkRuntime
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from literate_ai.storage.cas import FileSystemCAS


class RetainedSdkMetadataBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.events = []
        self.valid = True
        self.documents = (
            {"schema": "fixture@1", "z": [1, {"b": "é", "a": True}]},
            {"schema": "fixture@2", "payload": "x" * 1024},
            {"schema": "fixture@3", "context": {"argv": ["owned"], "value": None}},
        )
        # Construct only the metadata writer, not a forged project/SDK admission.
        # All currentness boundaries below are explicit stand-ins; CAS is real.
        self.session = object.__new__(RetainedProjectSdkRuntime)
        self.session.store = FileSystemCAS(self.root / "cas")
        self.session.policy = SimpleNamespace(bindings=())
        self.session.receiver_context = None
        self.session._metadata_byte_limit = 8192
        self.session.require_current = self.current
        self.real_put = self.session.store.put_bytes
        self.writes = []
        self.enterContext(patch.object(self.session.store, "put_bytes", self.put))

    def current(self):
        self.events.append("current")
        if not self.valid:
            raise ValueError("fixture-authority-changed")

    def put(self, content, **options):
        self.events.append("write")
        reference = self.real_put(content, **options)
        self.writes.append((content, reference))
        return reference

    def factory(self):
        sequence = sum(event == "open" for event in self.events) + 1
        self.events.append("open")
        try:
            yield from self.documents
        finally:
            self.events.append(("closed", sequence))

    def call(self, factory=None, count=None):
        return self.session.record_many(
            self.factory if factory is None else factory,
            count=len(self.documents) if count is None else count,
        )

    def retained_bytes(self, identities):
        for (content, reference), identity in zip(self.writes, identities, strict=True):
            self.session.store.verify(reference)
            self.assertEqual(identity.uri, reference.identity)
            self.assertEqual(self.session.store.get_bytes(reference), content)
        return tuple(content for content, _reference in self.writes)

    def test_batch_and_original_per_record_path_retain_identical_canonical_bytes(self):
        identities = self.call()
        batch = self.retained_bytes(identities)
        self.assertEqual(
            identities, tuple(canonical_identity(d) for d in self.documents)
        )
        self.assertEqual(batch, tuple(canonical_json_bytes(d) for d in self.documents))
        self.assertEqual(self.events.count("current"), 2)
        self.writes.clear()
        self.events.clear()
        legacy = tuple(self.session.record(d) for d in self.documents)
        self.assertEqual(legacy, identities)
        self.assertEqual(self.retained_bytes(legacy), batch)
        self.assertEqual(self.events.count("current"), len(self.documents))

    def test_overflow_closes_abandoned_factory_before_any_write_and_replays_guarded(
        self,
    ):
        self.session._metadata_byte_limit = len(canonical_json_bytes(self.documents[0]))
        with self.assertLogs(
            "literate_ai.adapters.retained_project_sdk_runtime", level="WARNING"
        ) as logged:
            identities = self.call()
        self.assertEqual(
            identities, tuple(canonical_identity(d) for d in self.documents)
        )
        self.assertEqual(
            self.retained_bytes(identities),
            tuple(canonical_json_bytes(d) for d in self.documents),
        )
        self.assertEqual(self.events.count("open"), 2)
        self.assertLess(self.events.index(("closed", 1)), self.events.index("write"))
        self.assertIn(("closed", 2), self.events)
        self.assertEqual(self.events.count("current"), len(self.documents))
        self.assertIn("metadata-batch-budget-exceeded", logged.output[0])
        self.assertEqual(len(logged.output), 1)

    def test_overflow_replay_rechecks_authority_before_second_record(self):
        self.session._metadata_byte_limit = 1

        def invalidate_after_write(content, **options):
            reference = self.put(content, **options)
            self.valid = False
            return reference

        with (
            patch.object(self.session.store, "put_bytes", invalidate_after_write),
            self.assertLogs(
                "literate_ai.adapters.retained_project_sdk_runtime", level="WARNING"
            ),
            self.assertRaisesRegex(ValueError, "fixture-authority-changed"),
        ):
            self.call()
        self.assertEqual(len(self.writes), 1)
        self.session.store.verify(self.writes[0][1])
        self.assertIn(("closed", 1), self.events)
        self.assertIn(("closed", 2), self.events)

    def test_short_or_long_factory_is_closed_and_refused_without_writes(self):
        for documents in (self.documents[:1], self.documents + ({"extra": True},)):
            with self.subTest(length=len(documents)):
                self.documents = documents
                self.events.clear()
                with self.assertRaisesRegex(ValueError, "metadata-count-differs"):
                    self.call(count=3)
                self.assertEqual(self.writes, [])
                self.assertIn(("closed", 1), self.events)
                self.assertNotIn("current", self.events)

    def test_invalid_factory_count_refuses_before_factory_or_cas(self):
        for count in (0, -1, True, 5, 1.0):
            with (
                self.subTest(count=count),
                self.assertRaisesRegex(ValueError, "bounded-metadata-factory-required"),
            ):
                self.call(count=count)
        self.assertEqual(self.events, [])
        self.assertEqual(self.writes, [])

    def test_wrong_cas_digest_or_size_cannot_return_batch_identities(self):
        for change in (dict(digest="0" * 64), dict(size=1)):
            with self.subTest(change=change):
                self.events.clear()
                self.writes.clear()

                def wrong_result(content, change=change, **options):
                    return replace(self.put(content, **options), **change)

                with (
                    patch.object(self.session.store, "put_bytes", wrong_result),
                    self.assertRaisesRegex(ValueError, "metadata-retention-differs"),
                ):
                    self.call()
                self.assertEqual(len(self.writes), 1)
                self.session.store.verify(self.writes[0][1])

    def test_receiver_custody_checked_before_every_write(self):
        def custody():
            self.events.append("custody")
            if len(self.writes) == 1:
                raise ValueError("fixture-custody-changed")

        self.session.receiver_context = SimpleNamespace(require_custody_current=custody)
        with self.assertRaisesRegex(ValueError, "fixture-custody-changed"):
            self.call()
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.events.count("custody"), 2)
        self.assertLess(self.events.index("custody"), self.events.index("write"))

    def test_full_currentness_refusal_before_writes(self):
        self.valid = False
        with self.assertRaisesRegex(ValueError, "fixture-authority-changed"):
            self.call()
        self.assertEqual(self.writes, [])
        self.assertIn(("closed", 1), self.events)

    def test_full_currentness_refusal_after_last_write_leaves_unadmitted_blobs(self):
        def invalidate_last(content, **options):
            reference = self.put(content, **options)
            if len(self.writes) == len(self.documents):
                self.valid = False
            return reference

        with (
            patch.object(self.session.store, "put_bytes", invalidate_last),
            self.assertRaisesRegex(ValueError, "fixture-authority-changed"),
        ):
            self.call()
        self.assertEqual(len(self.writes), len(self.documents))
        for _content, reference in self.writes:
            self.session.store.verify(reference)
        self.assertEqual(self.events[-1], "current")


if __name__ == "__main__":
    unittest.main()
