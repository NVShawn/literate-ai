"""Interoperable DSSE framing and real trusted-key signature verification."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from literate_ai.security.evidence import (
    DsseEnvelope,
    DsseError,
    DsseSignature,
    Ed25519EvidenceSigner,
    verify_ed25519_envelope,
)

# Public RFC 8032 section 7.1 test key, never an operational credential.
_SEED = bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
)
_MEDIA = "application/vnd.in-toto+json"


class Ed25519EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.signer = Ed25519EvidenceSigner(_SEED)
        self.second = Ed25519EvidenceSigner(b"\x02" * 32)
        self.payload = b"retained output"
        self.envelope = self.signer.sign(_MEDIA, self.payload)

    def verify(self, envelope, *, keys=None, minimum=1, media=_MEDIA):
        return verify_ed25519_envelope(
            envelope,
            trusted_public_keys=(self.signer.public_key,) if keys is None else keys,
            expected_payload_type=media,
            minimum_signatures=minimum,
        )

        # Predicate parsing and duplicate-field refusal belong to the next layer.

    def test_untrusted_signer_cannot_gain_trust_through_key_id(self):
        other = self.second.sign(_MEDIA, self.payload)
        forged_hint = replace(other.signatures[0], key_id=self.signer.key_identity)
        wire = replace(other, signatures=(forged_hint,)).to_dict()
        wire["authenticated"] = True
        with self.assertRaisesRegex(DsseError, "threshold-unmet"):
            self.verify(DsseEnvelope.from_bytes(json.dumps(wire).encode()))

    def test_payload_type_payload_and_signature_substitution_refuse(self):
        for envelope in (
            replace(self.envelope, payload=b"retained\toutput"),
            replace(self.envelope, payload_type="application/json"),
            replace(self.envelope, signatures=(DsseSignature(b"\0" * 64),)),
            replace(self.envelope, signatures=()),
        ):
            with self.subTest(envelope=envelope):
                with self.assertRaises(DsseError):
                    self.verify(envelope)
        with self.assertRaisesRegex(DsseError, "threshold-unmet"):
            self.verify(
                replace(self.envelope, payload_type="application/json"),
                media="application/json",
            )

    def test_threshold_counts_distinct_verified_keys_not_signatures_or_hints(self):
        keys = (self.signer.public_key, self.second.public_key)
        repeated = replace(
            self.envelope,
            signatures=(
                self.envelope.signatures[0],
                replace(self.envelope.signatures[0], key_id=self.second.key_identity),
            ),
        )
        with self.assertRaisesRegex(DsseError, "threshold-unmet"):
            self.verify(repeated, keys=keys, minimum=2)
        combined = replace(
            self.envelope,
            signatures=(
                *self.envelope.signatures,
                *self.second.sign(_MEDIA, self.payload).signatures,
            ),
        )
        result = self.verify(combined, keys=keys, minimum=2)
        self.assertEqual(len(result.signer_key_identities), 2)
        with self.assertRaisesRegex(DsseError, "threshold-invalid"):
            self.verify(combined, keys=(keys[0], keys[0]), minimum=2)
