"""Interoperable DSSE framing and real trusted-key signature verification."""

from __future__ import annotations

import base64
import json
import unittest
from dataclasses import replace
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from literate_ai.security.evidence import (
    DsseEnvelope,
    DsseError,
    DsseSignature,
    Ed25519EvidenceSigner,
    preauthentication_bytes,
    verify_ed25519_envelope,
)
from literate_ai.security.evidence.dsse import (
    MAX_ENVELOPE_BYTES,
    MAX_PAYLOAD_BYTES,
    MAX_SIGNATURES,
)

# Public RFC 8032 section 7.1 test key, never an operational credential.
_SEED = bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
)
_PUBLIC = bytes.fromhex(
    "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
)
_MEDIA = "application/vnd.in-toto+json"


class DsseEnvelopeTests(unittest.TestCase):
    def test_reference_pae_vector_and_utf8_byte_lengths(self):
        self.assertEqual(
            preauthentication_bytes("http://example.com/HelloWorld", b"hello world"),
            b"DSSEv1 29 http://example.com/HelloWorld 11 hello world",
        )
        self.assertEqual(
            preauthentication_bytes("é", b"\0\n"),
            b"DSSEv1 2 \xc3\xa9 2 \0\n",
        )
        self.assertEqual(preauthentication_bytes("", b""), b"DSSEv1 0  0 ")

    def test_standard_and_urlsafe_base64_preserve_binary_content(self):
        for encode in (base64.b64encode, base64.urlsafe_b64encode):
            wire = {
                "payload": encode(b"\xfb\xff").decode("ascii"),
                "payloadType": _MEDIA,
                "signatures": [{"sig": encode(b"\xff\xfb").decode("ascii")}],
            }
            envelope = DsseEnvelope.from_dict(wire)
            self.assertEqual(envelope.payload, b"\xfb\xff")
            self.assertEqual(envelope.signatures[0].signature, b"\xff\xfb")
            self.assertEqual(DsseEnvelope.from_bytes(envelope.to_bytes()), envelope)

    def test_external_extension_fields_are_ignored_not_treated_as_authority(self):
        wire = {
            "payload": "",
            "payloadType": "",
            "authenticated": True,
            "signatures": [{"sig": "", "keyid": "", "trusted": True}],
        }
        envelope = DsseEnvelope.from_dict(wire)
        self.assertEqual(envelope, DsseEnvelope("", b"", (DsseSignature(b""),)))
        self.assertNotIn("authenticated", envelope.to_dict())
        self.assertEqual(envelope.signatures[0].to_dict(), {"sig": ""})

    def test_malformed_or_ambiguous_json_refuses(self):
        cases = (
            b'{"payload":"","payload":"WA==","payloadType":"x","signatures":[]}',
            b'{"payload":"","payloadType":"x","signatures":[],"extra":NaN}',
            b"\xff",
            b"[" * 2000 + b"]" * 2000,
            b'{"extra":' + b"9" * 10000 + b"}",
            b"null",
        )
        for content in cases:
            with self.subTest(content=content[:50]):
                with self.assertRaises(DsseError):
                    DsseEnvelope.from_bytes(content)

    def test_missing_fields_invalid_types_and_bad_base64_refuse(self):
        wire = DsseEnvelope(_MEDIA, b"body", (DsseSignature(b"sig"),)).to_dict()
        for field in wire:
            with self.subTest(missing=field):
                with self.assertRaises(DsseError):
                    DsseEnvelope.from_dict(
                        {k: v for k, v in wire.items() if k != field}
                    )
        changes = (
            {"payload": True},
            {"payload": "!"},
            {"payload": "YQ"},
            {"payloadType": None},
            {"payloadType": "\ud800"},
            {"signatures": ""},
            {"signatures": [{}]},
            {"signatures": [{"sig": "", "keyid": False}]},
            {"signatures": [{"sig": "not base64"}]},
            {"signatures": [{"sig": ""}] * (MAX_SIGNATURES + 1)},
        )
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaises(DsseError):
                    DsseEnvelope.from_dict({**wire, **change})

    def test_size_limits_precede_payload_decode_and_json_parse(self):
        with mock.patch("literate_ai.security.evidence.dsse.json.loads") as parser:
            with self.assertRaises(DsseError):
                DsseEnvelope.from_bytes(b" " * (MAX_ENVELOPE_BYTES + 1))
            parser.assert_not_called()
        wire = {
            "payload": "A" * (4 * ((MAX_PAYLOAD_BYTES + 2) // 3) + 1),
            "payloadType": _MEDIA,
            "signatures": [],
        }
        with mock.patch(
            "literate_ai.security.evidence.dsse.base64.b64decode"
        ) as decode:
            with self.assertRaises(DsseError):
                DsseEnvelope.from_dict(wire)
            decode.assert_not_called()
        with self.assertRaises(DsseError):
            DsseEnvelope(_MEDIA, b"x" * (MAX_PAYLOAD_BYTES + 1), ())


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

    def test_external_signer_and_verifier_agree_on_exact_pae(self):
        self.assertEqual(self.signer.public_key, _PUBLIC)
        # Independent framing literal ensures both wrappers cannot share a framing bug.
        message = b"DSSEv1 28 application/vnd.in-toto+json 15 retained output"
        independent = Ed25519PrivateKey.from_private_bytes(_SEED)
        independent.public_key().verify(self.envelope.signatures[0].signature, message)
        incoming = DsseEnvelope(
            _MEDIA, self.payload, (DsseSignature(independent.sign(message)),)
        )
        verified = self.verify(incoming)
        self.assertIs(verified.payload, incoming.payload)
        self.assertEqual(verified.signer_key_identities, (self.signer.key_identity,))

    def test_verified_bytes_are_not_reparsed_or_reserialized(self):
        payload = b'{ "same": 1, "same": 2 }\n'
        signed = self.signer.sign(_MEDIA, payload)
        incoming = DsseEnvelope.from_bytes(signed.to_bytes())
        with mock.patch("json.loads", side_effect=AssertionError("unexpected parse")):
            verified = self.verify(incoming)
        self.assertIs(verified.payload, incoming.payload)
        self.assertEqual(verified.payload, payload)
        # Predicate parsing and duplicate-field refusal belong to the next layer.

    def test_untrusted_signer_cannot_gain_trust_through_key_id(self):
        other = self.second.sign(_MEDIA, self.payload)
        forged_hint = replace(other.signatures[0], key_id=self.signer.key_identity)
        wire = replace(other, signatures=(forged_hint,)).to_dict()
        wire["authenticated"] = True
        with self.assertRaisesRegex(DsseError, "threshold-unmet"):
            self.verify(DsseEnvelope.from_bytes(json.dumps(wire).encode()))

    def test_valid_signature_does_not_depend_on_unauthenticated_key_hint(self):
        signature = replace(self.envelope.signatures[0], key_id="unrelated-hint")
        verified = self.verify(replace(self.envelope, signatures=(signature,)))
        self.assertEqual(verified.signer_key_identities, (self.signer.key_identity,))

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

    def test_invalid_or_empty_trust_and_thresholds_fail_closed(self):
        for keys in ((), (b"short",), [self.signer.public_key]):
            with self.subTest(keys=keys):
                with self.assertRaisesRegex(DsseError, "trusted-keys-invalid"):
                    self.verify(self.envelope, keys=keys)
        for threshold in (0, -1, True, 1.0, 2):
            with self.subTest(threshold=threshold):
                with self.assertRaisesRegex(DsseError, "threshold-invalid"):
                    self.verify(self.envelope, minimum=threshold)
        with self.assertRaisesRegex(DsseError, "expected-type-required"):
            self.verify(self.envelope, media="")

    def test_private_key_material_is_not_part_of_signer_representation(self):
        self.assertNotIn(_SEED.hex(), repr(self.signer))
        self.assertNotIn(repr(_SEED), repr(self.signer))
        with self.assertRaisesRegex(DsseError, "private-key-invalid"):
            Ed25519EvidenceSigner(b"short")
