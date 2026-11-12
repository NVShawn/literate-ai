"""Real publication/custody refusals with explicit native admission fixtures.

Current-plan, projection and semantic native comparison are injected as in the
publication controls. Archive/filesystem changes and retirement checks are real;
these tests confer no genuine authored qualification or consumer admission.
"""

import unittest
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.authored_qualification_capture import (
    reopen_authored_product_membership,
)
from literate_ai.adapters.retained_authored_provider import (
    open_retained_authored_provider,
)
from literate_ai.contracts.identity import canonical_identity
from tests.critical import test_authored_qualification_publication as fixtures

MODULE = "literate_ai.adapters.retained_authored_provider."


class RetainedAuthoredProviderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AuthoredQualificationPublicationTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    @contextmanager
    def opened(self):
        f = self.fixture
        with f.admitted():
            published = f.apply()

            def reopen(_projection, reader, identity, **_kwargs):
                return SimpleNamespace(
                    membership=reopen_authored_product_membership(reader, identity)
                )

            with (
                patch(
                    MODULE + "open_current_authored_qualification_projection",
                    side_effect=lambda *_a, **_k: nullcontext(
                        SimpleNamespace(
                            require_unchanged=f.current.require_unchanged
                        )
                    ),
                ),
                patch(
                    MODULE + "reopen_projected_authored_qualification",
                    side_effect=reopen,
                ),
                open_retained_authored_provider(
                    f.current,
                    published.reference.attempt,
                    expected_publication_identity=published.reference.identity,
                ) as owner,
            ):
                yield owner

    def test_live_owner_expires_and_a_copy_cannot_borrow_its_guard(self):
        with self.opened() as owner:
            owner.require_unchanged()
            with self.assertRaisesRegex(ValueError, "original_receiving_owner"):
                replace(owner).require_unchanged()
        with self.assertRaisesRegex(ValueError, "receiving_owner_expired"):
            owner.require_unchanged()

    def test_changed_archive_bytes_refuse_without_restoring_or_deleting_them(self):
        with self.assertRaisesRegex(ValueError, "receiving_archive_changed"):
            with self.opened() as owner:
                reference = owner.publication.reference.archive
                path = (
                    self.fixture.root
                    / owner.publication.archive_store_path
                    / "blobs/sha256"
                    / reference.digest[:2]
                    / reference.digest
                )
                before = path.read_bytes()
                path.write_bytes(bytes([before[0] ^ 1]) + before[1:])
                owner.require_unchanged()
        self.assertNotEqual(path.read_bytes(), before)

    def test_recreated_run_path_is_not_accepted_as_retired(self):
        with self.assertRaisesRegex(ValueError, "run_resources_not_retired"):
            with self.opened() as owner:
                path = self.fixture.paths[0]
                path.mkdir()
                owner.require_unchanged()
        self.assertTrue(path.is_dir())

    def test_current_original_authority_is_rechecked(self):
        with self.assertRaisesRegex(ValueError, "current authority changed"):
            with self.opened() as owner:
                self.fixture.active = False
                owner.require_unchanged()

    def test_independent_reference_identity_is_required_before_projection(self):
        f = self.fixture
        with f.admitted():
            published = f.apply()
            with (
                patch(MODULE + "open_current_authored_qualification_projection") as native,
                self.assertRaisesRegex(ValueError, "reference_mismatch"),
                open_retained_authored_provider(
                    f.current,
                    published.reference.attempt,
                    expected_publication_identity=canonical_identity("foreign"),
                ),
            ):
                self.fail("foreign reference restored a provider")
            native.assert_not_called()
