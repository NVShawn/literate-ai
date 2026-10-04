from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.source_to_specification import (
    SourceToSpecificationError,
    parse_source_attestation,
    read_local_trust_key,
    sign_source_inventory,
    verify_source_attestation,
)

KEY = b"local-bootstrap-key-material-32-bytes-minimum"


class LocalAttestationTests(unittest.TestCase):
    def test_source_attestation_is_exact_and_tamper_evident(self) -> None:
        attestation = sign_source_inventory(
            "sha256:source-a", signer="alice", machine="builder-1", key=KEY
        )
        parsed = parse_source_attestation(attestation.to_dict())
        verify_source_attestation(
            parsed, expected_source_snapshot_id="sha256:source-a", key=KEY
        )
        with self.assertRaisesRegex(SourceToSpecificationError, "another snapshot"):
            verify_source_attestation(
                parsed, expected_source_snapshot_id="sha256:source-b", key=KEY
            )
        tampered = attestation.to_dict()
        tampered["signature"] = "hmac-sha256:" + "0" * 64
        with self.assertRaisesRegex(SourceToSpecificationError, "signature"):
            verify_source_attestation(
                parse_source_attestation(tampered),
                expected_source_snapshot_id="sha256:source-a",
                key=KEY,
            )
        with self.assertRaises(SourceToSpecificationError):
            parse_source_attestation({**attestation.to_dict(), "signer": 7})

    def test_key_reader_rejects_weak_and_symbolic_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            weak = root / "weak.key"
            weak.write_bytes(b"short")
            with self.assertRaisesRegex(SourceToSpecificationError, "32 bytes"):
                read_local_trust_key(weak)
            strong = root / "strong.key"
            strong.write_bytes(KEY)
            alias = root / "alias.key"
            try:
                alias.symlink_to(strong)
            except OSError:
                self.skipTest("symbolic links are unavailable")
            with self.assertRaisesRegex(SourceToSpecificationError, "regular file"):
                read_local_trust_key(alias)


if __name__ == "__main__":
    unittest.main()
