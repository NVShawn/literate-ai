"""Authorized retention roots cover every observed object under one graph budget."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from literate_ai.application.evidence_resolution import ConfiguredEvidenceResolver
from literate_ai.application.evidence_verification import verify_retained_evidence_graph
from literate_ai.security.evidence import (
    STATEMENT_MEDIA_TYPE,
    DsseEnvelope,
    DsseError,
    Ed25519EvidenceSigner,
    EvidenceLocator,
    EvidenceStatement,
    EvidenceTrustError,
)
from literate_ai.security.evidence.graph import (
    EvidenceGraphLimits,
    EvidenceVerificationState,
)
from literate_ai.security.evidence.retention import SignedEvidenceRetention
from literate_ai.security.evidence.storage import (
    EvidenceNotFoundError,
    EvidenceReadLimits,
    EvidenceStorageError,
)
from tests.support.fixtures_test_evidence_graph import _Graph


def _claims(graph, *, signer=None, locators=None):
    signer = signer or graph.signer
    return tuple(
        SignedEvidenceRetention(
            locator,
            signer.sign(
                STATEMENT_MEDIA_TYPE, EvidenceStatement(locator).to_bytes()
            ).to_bytes(),
        )
        for locator in (graph.locators() if locators is None else locators)
    )


def _verify(graph, **kwargs):
    arguments = dict(
        requirements=tuple(graph.requirements.values()),
        resolver=graph.resolver,
        retention=_claims(graph),
        policy=graph.policy,
        current_state=graph.state,
    )
    arguments.update(kwargs)
    return verify_retained_evidence_graph(graph.refs["matrix"], **arguments)


class EvidenceRetentionTests(unittest.TestCase):
    def refuses(self, graph, code, **kwargs):
        with self.assertRaises((EvidenceTrustError, EvidenceStorageError)) as caught:
            _verify(graph, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_complete_detached_roots_bind_exact_bytes_and_every_shared_run_scope(self):
        graph = _Graph()
        claims = _claims(graph)
        result = _verify(graph, retention=claims)
        self.assertEqual(len(result.retention), len(result.graph.objects))
        by_object = {item.claim.locator.subject: item for item in result.retention}
        for obj in result.graph.objects:
            item = by_object[obj.reference]
            self.assertEqual(item.claim.locator, obj.locator)
            self.assertIn(item.claim, claims)
            self.assertTrue(item.runs)
            for _, checked in item.runs:
                self.assertEqual(checked.authenticated.statement.predicate, obj.locator)
                self.assertEqual(checked.checked_at, result.graph.final_state.now)
        # Both platform runs and the derivation itself reference this shared envelope.
        self.assertEqual(
            {ref for ref, _ in by_object[graph.refs["derive"]].runs},
            {graph.refs[name] for name in ("derive", "linux", "windows")},
        )
        self.assertEqual(
            result.identity, _verify(graph, retention=tuple(reversed(claims))).identity
        )

    def test_missing_duplicate_extra_and_changed_locator_hints_fail_before_io(self):
        graph = _Graph()
        claims = _claims(graph)
        self.refuses(graph, "evidence.storage.not-found", retention=claims[:-1])
        self.refuses(
            graph, "evidence.retention.claim-duplicate", retention=(*claims, claims[0])
        )
        extra = EvidenceLocator("local", graph.blob(b"not in plan"), 1000)
        self.refuses(
            graph,
            "evidence.storage.locator-mismatch",
            retention=(*claims, *_claims(graph, locators=(extra,))),
        )
        changed = replace(
            claims[0], locator=replace(claims[0].locator, retained_until=999)
        )
        self.refuses(
            graph,
            "evidence.retention.locator-mismatch",
            retention=(changed, *claims[1:]),
        )
        self.assertFalse(graph.reads)

    def test_untrusted_or_unsigned_claim_cannot_route_any_read(self):
        for signer in (Ed25519EvidenceSigner(b"x" * 32), None):
            graph = _Graph()
            if signer is None:
                claims = _claims(graph)
                envelope = DsseEnvelope.from_bytes(claims[0].envelope)
                claims = (
                    replace(
                        claims[0], envelope=replace(envelope, signatures=()).to_bytes()
                    ),
                    *claims[1:],
                )
            else:
                claims = _claims(graph, signer=signer)
            with self.assertRaises(DsseError):
                _verify(graph, retention=claims)
            self.assertFalse(graph.reads)

    def test_store_scope_and_all_referencing_targets_are_required(self):
        for rule in ("store", "target"):
            graph = _Graph()
            original = graph.policy.signers[0]
            scoped = (
                replace(original, store_ids=("different",))
                if rule == "store"
                else replace(original, targets=(graph.contexts["matrix"].target,))
            )
            self.refuses(
                graph,
                "evidence.trust.signer-threshold-unmet",
                policy=replace(graph.policy, signers=(scoped,)),
            )
            self.assertFalse(graph.reads)

    def test_retention_deadline_is_rechecked_after_all_reads(self):
        graph = _Graph()
        claims = _claims(
            graph,
            locators=tuple(
                replace(loc, retained_until=300) for loc in graph.locators()
            ),
        )
        graph.state.side_effect = [graph.snapshot, replace(graph.snapshot, now=211)]
        self.refuses(graph, "evidence.trust.retention-too-short", retention=claims)
        self.assertEqual(len(graph.reads), len(graph.locators()))
        graph = _Graph()
        claims = _claims(
            graph,
            locators=tuple(
                replace(loc, retained_until=299) for loc in graph.locators()
            ),
        )
        self.refuses(graph, "evidence.trust.retention-too-short", retention=claims)
        self.assertFalse(graph.reads)

    def test_retention_signer_revoked_after_reads_while_run_signer_stays_valid(self):
        graph = _Graph()
        original = graph.policy.signers[0]
        store_signer = Ed25519EvidenceSigner(b"s" * 32)
        run_rule = replace(
            original,
            predicate_types=tuple(
                t for t in original.predicate_types if t != EvidenceLocator.SCHEMA
            ),
            store_ids=(),
        )
        store_rule = replace(
            original,
            public_key=store_signer.public_key,
            issuer="local:store",
            predicate_types=(EvidenceLocator.SCHEMA,),
        )
        policy = replace(
            graph.policy,
            signers=tuple(
                sorted((run_rule, store_rule), key=lambda r: r.key_identity.uri)
            ),
        )
        graph.state.side_effect = [
            graph.snapshot,
            EvidenceVerificationState(
                211,
                replace(
                    graph.snapshot.revocations, issued_at=211, issuers=("local:store",)
                ),
            ),
        ]
        self.refuses(
            graph,
            "evidence.trust.signer-threshold-unmet",
            policy=policy,
            retention=_claims(graph, signer=store_signer),
        )
        self.assertEqual(len(graph.reads), len(graph.locators()))

    def test_signed_claim_is_not_a_substitute_for_actual_blob_availability(self):
        graph = _Graph()
        claims = _claims(graph)
        del graph.contents[graph.records["derive"].journal.identity]
        self.refuses(graph, "evidence.storage.not-found", retention=claims)
        self.assertEqual(graph.state.call_count, 1)

    def test_only_signed_configured_mirrors_can_supply_an_object(self):
        graph = _Graph()
        locators = graph.locators()
        absent = Mock()
        absent.get_bytes.side_effect = EvidenceNotFoundError()
        resolver = ConfiguredEvidenceResolver({"a-missing": absent, "local": graph})
        mirror = replace(locators[0], store_id="a-missing")
        policy = replace(
            graph.policy,
            signers=(
                replace(graph.policy.signers[0], store_ids=("a-missing", "local")),
            ),
        )
        result = _verify(
            graph,
            resolver=resolver,
            policy=policy,
            retention=_claims(graph, locators=(*locators, mirror)),
        )
        absent.get_bytes.assert_called_once_with(mirror.subject)
        self.assertTrue(
            all(item.locator.store_id == "local" for item in result.graph.objects)
        )
        self.assertEqual(len(result.retention), len(locators) + 1)

    def test_detached_proof_objects_and_bytes_share_the_graph_budget(self):
        graph = _Graph()
        claims = _claims(graph)
        references = [loc.subject for loc in graph.locators()] + [
            item.reference for item in claims
        ]
        maximum = max(ref.size for ref in references)
        total = sum(ref.size for ref in references)
        for limits, code in (
            (
                EvidenceReadLimits(maximum, total - 1, len(references)),
                "evidence.storage.total-limit",
            ),
            (
                EvidenceReadLimits(maximum, total, len(references) - 1),
                "evidence.storage.object-limit",
            ),
        ):
            with patch(
                "literate_ai.application.evidence_verification.check_retention_evidence"
            ) as check:
                self.refuses(
                    graph, code, retention=claims, limits=EvidenceGraphLimits(limits)
                )
                check.assert_not_called()
            self.assertFalse(graph.reads)
        self.assertEqual(
            len(
                _verify(
                    graph,
                    retention=claims,
                    limits=EvidenceGraphLimits(
                        EvidenceReadLimits(maximum, total, len(references))
                    ),
                ).retention
            ),
            len(claims),
        )

    def test_signature_budget_is_shared_by_runs_claims_and_both_passes(self):
        graph = _Graph()
        # Four run checks plus 18 referring-run/object claim checks per pass.
        self.refuses(
            graph,
            "evidence.graph.signature-limit",
            limits=EvidenceGraphLimits(maximum_signature_checks=43),
        )
        self.assertEqual(graph.state.call_count, 2)
        self.assertEqual(
            len(
                _verify(
                    graph, limits=EvidenceGraphLimits(maximum_signature_checks=44)
                ).graph.runs
            ),
            4,
        )


if __name__ == "__main__":
    unittest.main()
