"""Security gates for source-to-specification dynamic observations."""

from __future__ import annotations

import tempfile
import unittest
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
    SourceTreeFingerprint,
)

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
