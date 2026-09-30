"""Trusted-store admission preserves single-use semantics across process races."""

from __future__ import annotations

import multiprocessing
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

from literate_ai.adapters.build_grant_consumption import (
    SQLiteBuildGrantConsumptionStore,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.security.build_admission import SingleUseBuildAdmission
from literate_ai.security.policy import (
    AuthorizationError,
    BuildAuthorization,
    BuildRequest,
    SecurityProfile,
)

NOW = datetime(2026, 9, 13, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


_RACE_BARRIER = None


def _initialize_race(barrier):
    global _RACE_BARRIER
    _RACE_BARRIER = barrier


def _consume(path, grant, request):
    _RACE_BARRIER.wait(timeout=10)
    try:
        SQLiteBuildGrantConsumptionStore(Path(path)).consume(grant, request)
        return "admitted"
    except AuthorizationError as exc:
        return exc.code


class BuildGrantConsumptionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name).resolve() / "admission.db"
        self.store = SQLiteBuildGrantConsumptionStore.initialize(self.path)
        self.request = BuildRequest(
            DIGEST,
            DIGEST,
            "fixture-builder",
            DIGEST,
            "fixture-profile",
            (),
            ("output",),
        )
        self.grant = BuildAuthorization(
            "grant-one",
            DIGEST,
            canonical_identity(self.request.to_dict()).uri,
            DIGEST,
            "fixture-actor",
            "test",
            SecurityProfile.CONSTRAINED,
            (),
            NOW,
            NOW + timedelta(hours=1),
        )

    def admission(self, verifier=None, clock=lambda: NOW):
        return SingleUseBuildAdmission(
            verifier if verifier is not None else Mock(), self.store, clock
        )

    def test_success_is_spent_across_reopen_and_changed_grant_bytes(self):
        self.admission().admit(self.grant, self.request)
        self.store = SQLiteBuildGrantConsumptionStore(self.path)
        with self.assertRaisesRegex(AuthorizationError, "already_consumed"):
            self.admission().admit(
                replace(self.grant, reason="different bytes"), self.request
            )

    def test_process_race_has_one_winner(self):
        with ProcessPoolExecutor(
            max_workers=4,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_initialize_race,
            initargs=(multiprocessing.get_context("spawn").Barrier(4),),
        ) as executor:
            futures = [
                executor.submit(_consume, str(self.path), self.grant, self.request)
                for _ in range(8)
            ]
            results = [future.result(timeout=60) for future in futures]
        self.assertEqual(results.count("admitted"), 1)
        self.assertEqual(results.count("security.authorization_already_consumed"), 7)

    def test_failed_initial_verification_does_not_spend(self):
        verifier = Mock()
        verifier.require_build_valid.side_effect = AuthorizationError(
            "security.authorization_revoked"
        )
        with self.assertRaisesRegex(AuthorizationError, "revoked"):
            self.admission(verifier).admit(self.grant, self.request)
        with self.assertRaisesRegex(AuthorizationError, "request_mismatch"):
            self.admission().admit(
                self.grant, replace(self.request, builder_id="different")
            )
        self.admission().admit(self.grant, self.request)

    def test_revocation_after_consumption_never_refunds(self):
        verifier = Mock()
        verifier.require_build_valid.side_effect = [
            None,
            AuthorizationError("security.authorization_revoked"),
        ]
        with self.assertRaisesRegex(AuthorizationError, "revoked"):
            self.admission(verifier).admit(self.grant, self.request)
        with self.assertRaisesRegex(AuthorizationError, "already_consumed"):
            self.admission().admit(self.grant, self.request)

    def test_expiry_after_consumption_never_refunds(self):
        clock = Mock(side_effect=[NOW, NOW + timedelta(hours=2)])
        with self.assertRaisesRegex(AuthorizationError, "expired"):
            self.admission(clock=clock).admit(self.grant, self.request)
        with self.assertRaisesRegex(AuthorizationError, "already_consumed"):
            self.admission().admit(self.grant, self.request)

    def test_store_loss_corruption_and_reinitialization_refuse(self):
        with self.assertRaises(AuthorizationError):
            SQLiteBuildGrantConsumptionStore.initialize(self.path)
        self.path.write_bytes(b"corrupt database")
        with self.assertRaises(AuthorizationError):
            self.admission().admit(self.grant, self.request)
        self.path.unlink()
        with self.assertRaises(AuthorizationError):
            self.admission().admit(self.grant, self.request)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
