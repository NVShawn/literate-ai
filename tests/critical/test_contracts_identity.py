"""Canonical identity and strict wire validation tests."""

from __future__ import annotations

import unittest

from literate_ai.contracts import (
    canonical_identity,
    canonical_json_bytes,
)


class CanonicalIdentityTests(unittest.TestCase):
    def test_canonical_fixture_is_stable(self) -> None:
        value = {"b": 2, "a": "é"}
        self.assertEqual(canonical_json_bytes(value), '{"a":"é","b":2}'.encode())
        self.assertEqual(
            canonical_identity(value).digest,
            "06c264c46ad5ada9493abd3aa2383fb205ae99d7d0bad40b03a43bfec8a1b8de",
        )


if __name__ == "__main__":
    unittest.main()
