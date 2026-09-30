"""Security gates for source-to-specification dynamic observations."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.security import (
    AuthorizationError,
    AuthorizationRevocationSet,
    LiveObservationExecutionAuthorizationVerifier,
    ObservationRequest,
    OriginAttestation,
    SecurityPolicy,
)
from literate_ai.source_to_specification import (
    AuthorizedDynamicObserver,
    SourceMutationError,
    SourceToSpecificationError,
    SourceTreeFingerprint,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
NOW = datetime(2026, 8, 2, tzinfo=UTC)


class Runner:
    runner_id = "runner:fixture@1"

    def __init__(self, source: Path, mutate: bool = False) -> None:
        self.source = source
        self.mutate = mutate
        self.calls = 0

    def run(self, request, authorization):
        self.calls += 1
        if self.mutate:
            self.source.write_text("mutated\n")
        return {
            "observations": [{"event": "started"}],
            "request_digest": authorization["request_digest"],
        }


def grant(source_root: Path):
    source_digest = SourceTreeFingerprint(source_root).digest
    policy = SecurityPolicy(policy_digest=DIGEST_C)
    classification = policy.classify(
        effective_revision_digest=DIGEST_B,
        attestations=(
            OriginAttestation(source_digest, "signer", "root", "signature", True),
        ),
        findings=(),
    )
    observation_request = ObservationRequest(
        effective_revision_digest=DIGEST_B,
        source_digests=(source_digest,),
        runner_id="runner:fixture@1",
        harness_digest=DIGEST_C,
        sandbox_profile="observation",
        requested_privileges=("processes",),
        allowed_outputs=("trace",),
    )
    authorization = policy.authorize_observation(
        classification,
        observation_request,
        actor="reviewer",
        reason="observe exact behavior",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    return observation_request, authorization


def observer(runner: Runner, provider=lambda: AuthorizationRevocationSet()):
    return AuthorizedDynamicObserver(
        runner,
        LiveObservationExecutionAuthorizationVerifier(provider),
    )


class AuthorizedObservationTests(unittest.TestCase):
    def test_exact_authorization_runs_and_records_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "main.py"
            source.write_text("print('ok')\n")
            runner = Runner(source)
            observation_request, authorization = grant(Path(temporary))
            result = observer(runner).observe(
                observation_request,
                authorization,
                source_root=temporary,
                now=NOW,
            )
            self.assertEqual(runner.calls, 1)
            self.assertEqual(result.authorization_id, authorization.authorization_id)
            self.assertTrue(result.identity.startswith("sha256:"))

    def test_expired_or_wrong_runner_fails_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "main.py"
            source.write_text("pass\n")
            runner = Runner(source)
            observation_request, authorization = grant(Path(temporary))
            with self.assertRaisesRegex(Exception, "expired"):
                observer(runner).observe(
                    observation_request,
                    authorization,
                    source_root=temporary,
                    now=NOW + timedelta(minutes=6),
                )
            self.assertEqual(runner.calls, 0)
            wrong = replace(observation_request, runner_id="runner:other@1")
            with self.assertRaises(SourceToSpecificationError):
                observer(runner).observe(
                    wrong, authorization, source_root=temporary, now=NOW
                )
            self.assertEqual(runner.calls, 0)

    def test_source_mutation_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "main.py"
            source.write_text("pass\n")
            observation_request, authorization = grant(Path(temporary))
            with self.assertRaises(SourceMutationError):
                observer(Runner(source, mutate=True)).observe(
                    observation_request,
                    authorization,
                    source_root=temporary,
                    now=NOW,
                )

    def test_grant_for_another_source_fails_before_execution(self) -> None:
        with (
            tempfile.TemporaryDirectory() as authorized_directory,
            tempfile.TemporaryDirectory() as actual_directory,
        ):
            authorized_root = Path(authorized_directory)
            actual_root = Path(actual_directory)
            (authorized_root / "main.py").write_text("print('authorized')\n")
            actual_source = actual_root / "main.py"
            actual_source.write_text("print('different')\n")
            observation_request, authorization = grant(authorized_root)
            runner = Runner(actual_source)

            with self.assertRaisesRegex(SourceToSpecificationError, "source"):
                observer(runner).observe(
                    observation_request,
                    authorization,
                    source_root=actual_root,
                    now=NOW,
                )

            self.assertEqual(runner.calls, 0)

    def test_default_fails_closed_and_live_revocation_wins_after_issuance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "main.py"
            source.write_text("pass\n")
            runner = Runner(source)
            request, authorization = grant(Path(temporary))

            with self.assertRaisesRegex(
                AuthorizationError,
                "live_observation_revocation_verifier_required",
            ):
                AuthorizedDynamicObserver(runner).observe(
                    request,
                    authorization,
                    source_root=temporary,
                    now=NOW,
                )
            self.assertEqual(runner.calls, 0)

            current = AuthorizationRevocationSet()
            live = observer(runner, lambda: current)
            current = current.revoke(
                authorization.authorization_id,
                actor="security",
                reason="source compromised after issuance",
            )
            with self.assertRaisesRegex(AuthorizationError, "revoked"):
                live.observe(
                    request,
                    authorization,
                    source_root=temporary,
                    now=NOW,
                )
            self.assertEqual(runner.calls, 0)


if __name__ == "__main__":
    unittest.main()
