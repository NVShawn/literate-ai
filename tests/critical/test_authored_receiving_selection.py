"""Typed authored product selection after complete Standard archive reopening.

Original projection/current-plan admission is an explicit fixture. Real archive
records, all-run stage checks, blob hashes and selected package membership are
reused from the authored reopening controls. No genuine authored generation,
native qualification, importer permission or receiving transaction is claimed.
"""

import unittest
from unittest.mock import patch

from literate_ai.adapters.authored_qualification_capture import (
    AuthoredQualificationRunCapture,
)
from literate_ai.adapters.authored_qualification_projection import (
    CurrentAuthoredQualificationProjection,
    reopen_projected_authored_run,
)
from literate_ai.adapters.qualification_capture import QualificationCaptureError
from literate_ai.contracts.identity import canonical_identity
from tests.critical import test_authored_qualification_reopening as fixtures

MODULE = "literate_ai.adapters.authored_qualification_projection."


class AuthoredReceivingSelectionTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.AuthoredQualificationReopeningTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.run = fixture.result.runs[0]
        self.reopened = None

        def guard(_owner):
            if not fixture.active:
                raise ValueError("fixture original projection expired")

        self.projection = CurrentAuthoredQualificationProjection(
            fixture.current,
            fixture.fixture.current_recipes,
            fixture.fixture.current_commands,
            guard,
        )

    def select(self, *, exports, run=None, reader=None):
        fixture = self.fixture

        def reopen(projection, source, products, *, max_package_bytes):
            self.assertIs(projection, self.projection)
            self.assertEqual(products, fixture.products.identity)
            self.reopened = fixture.reopen(max_package_bytes=max_package_bytes)
            return self.reopened

        with patch(MODULE + "reopen_projected_authored_qualification", reopen):
            return reopen_projected_authored_run(
                self.projection,
                fixture.reader() if reader is None else reader,
                fixture.products.identity,
                run_identity=self.run.request.run_identity if run is None else run,
                exports_identity=exports,
                max_package_bytes=4096,
            )

    def exports(self):
        membership = self.fixture.reopen().membership
        return dict(membership.exports)[self.run.request.run_identity]

    def test_selected_run_preserves_authored_origin_and_complete_packages(self):
        expected = self.exports()
        captured = self.select(exports=expected.identity)
        self.assertIs(type(captured), AuthoredQualificationRunCapture)
        self.assertEqual(captured.run, self.run)
        self.assertEqual(captured.exports, expected)
        self.assertEqual(
            {reference for reference, _ in captured.blobs},
            {
                export.blob
                for manifest in expected.graph.manifests
                for export in manifest.exports
            },
        )
        self.assertEqual(len(self.reopened.membership.result.runs), 2)
        self.assertFalse(hasattr(captured, "promotion"))

    def test_foreign_or_nonselected_run_export_pair_refuses(self):
        membership = self.fixture.reopen().membership
        expected = dict(membership.exports)[self.run.request.run_identity]
        for run, exports in (
            (canonical_identity("foreign run"), expected.identity),
            (self.run.request.run_identity, canonical_identity("foreign exports")),
        ):
            with self.subTest(run=run, exports=exports):
                with self.assertRaisesRegex(
                    QualificationCaptureError, "receiving_selection_mismatch"
                ):
                    self.select(run=run, exports=exports)

    def test_failed_other_clean_run_cannot_be_hidden_by_selection(self):
        expected = self.exports()
        self.fixture.fail_second_comparison = True
        with self.assertRaisesRegex(QualificationCaptureError, "comparison failed"):
            self.select(exports=expected.identity)

    def test_current_projection_expiry_during_selected_byte_read_refuses(self):
        expected = self.exports()
        reader = self.fixture.reader()
        read = reader.read_bytes

        def expire(identity):
            result = read(identity)
            self.fixture.active = False
            return result

        reader.read_bytes = expire
        with self.assertRaisesRegex(ValueError, "original projection expired"):
            self.select(exports=expected.identity, reader=reader)

    def test_passive_projection_cannot_select_packages(self):
        with (
            patch(MODULE + "reopen_projected_authored_qualification") as reopen,
            self.assertRaises(TypeError),
        ):
            reopen_projected_authored_run(
                self.fixture.current.plan,
                self.fixture.reader(),
                self.fixture.products.identity,
                run_identity=self.run.request.run_identity,
                exports_identity=canonical_identity("exports"),
                max_package_bytes=4096,
            )
        reopen.assert_not_called()
