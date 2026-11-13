"""Authored import/archive/materialization contracts, not native admission.

The shared fixture explicitly injects generation/publication/native-plan and
comparison admission. Every returned archive/product, independent review read,
manifest checksum and package publication is real and bounded.
"""

import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.authored_qualification_capture import (
    AuthoredQualificationRunCapture,
)
from literate_ai.adapters.retained_authored_cargo_import_v2 import (
    verify_retained_authored_cargo_archive_v2,
)
from literate_ai.adapters.retained_cargo_files_v2 import read_retained_cargo_files_v2
from literate_ai.adapters.retained_cargo_import import verify_retained_cargo_archive
from literate_ai.adapters.retained_cargo_import_v2 import verify_retained_cargo_archive_v2
from literate_ai.adapters.retained_cargo_materialization_v2 import (
    materialize_retained_cargo_archive_v2,
)
from literate_ai.adapters.retained_cargo_scratch_v2 import reserve_retained_cargo_scratch_v2
from literate_ai.adapters.retained_cargo_wrapper_v2 import retained_cargo_wrapper_context_v2
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from tests.support.fixtures_retained_authored_cargo import authored_cargo_fixture


@unittest.skipUnless(
    os.name != "nt"
    and all(shutil.which(tool) for tool in ("cargo", "rustc", "git", "make")),
    "requires native POSIX Cargo/Rust and retained Git/Make adoption",
)
class RetainedAuthoredCargoImportTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory(prefix="auth-cargo-"))
        self.fixture = self.enterContext(authored_cargo_fixture(Path(root).resolve()))

    def test_complete_authored_archive_uses_both_public_readers_unchanged(self):
        f = self.fixture
        for reader in (
            verify_retained_authored_cargo_archive_v2,
            verify_retained_cargo_archive_v2,
        ):
            with self.subTest(reader=reader.__name__):
                plan, capture = reader(**f.calls)
                self.assertEqual(plan, f.reviewed.plan)
                self.assertIs(type(capture), AuthoredQualificationRunCapture)
                self.assertEqual(capture.run, f.run)
                self.assertEqual(capture.exports, f.exports)
                self.assertFalse(hasattr(capture, "promotion"))

    def test_independent_importer_review_and_store_refuse_before_transport(self):
        f = self.fixture
        for changes in (
            {"reviewed_binding_identity": canonical_identity("unreviewed")},
            {"configured_store_id": "unconfigured-store"},
        ):
            with self.subTest(changes=tuple(changes)), self.assertRaisesRegex(
                ValueError, "importer-authority-mismatch"
            ):
                verify_retained_cargo_archive_v2(
                    **{**f.calls, "read_current": lambda: replace(f.current, **changes)}
                )
        self.assertEqual(f.state.reads, [])

    def test_expired_authored_provider_after_transport_yields_no_products(self):
        f = self.fixture

        def expired(*args):
            content = f.read_archive(*args)
            f.state.active = False
            return content

        with self.assertRaisesRegex(ValueError, "original authored owner expired"):
            verify_retained_cargo_archive_v2(**{**f.calls, "read_archive": expired})

    def test_source_derived_archive_cannot_replace_authored_products(self):
        f = self.fixture
        with self.assertRaises(ValueError):
            verify_retained_cargo_archive_v2(
                **{**f.calls, "read_archive": lambda *_args: f.source_archive}
            )

    def test_authored_dto_cannot_enter_source_promotion_front_door(self):
        f = self.fixture
        with self.assertRaisesRegex(TypeError, "typed current input snapshot"):
            verify_retained_cargo_archive(
                f.reviewed.binding.common,
                canonical_json_bytes(f.reviewed.plan.to_dict()),
                **f.calls,
            )
        self.assertEqual(f.state.reads, [])

    def test_authored_packages_use_shared_exclusive_live_source_publication(self):
        f = self.fixture
        with retained_cargo_wrapper_context_v2(f.reviewed) as context:
            with reserve_retained_cargo_scratch_v2(context) as scratch:
                files = read_retained_cargo_files_v2(
                    f.reviewed, manifest_state="after", package_state="absent"
                )
                materialized = materialize_retained_cargo_archive_v2(
                    files, scratch=scratch, **f.calls
                )
                expected = {
                    f.reviewed.project.root / destination
                    for _, destination in f.reviewed.binding.common.destinations
                }
                self.assertEqual({p.root for p in materialized.packages}, expected)
                self.assertIs(type(materialized.capture), AuthoredQualificationRunCapture)
                materialized.require_unchanged()
                f.state.active = False
                with self.assertRaisesRegex(ValueError, "original authored owner expired"):
                    materialized.require_unchanged()
        with self.assertRaisesRegex(ValueError, "scratch-operation-ended"):
            materialized.require_unchanged()
