"""Cache observations cannot invent hits or expose uncontrolled configuration."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.compiler_cache import (
    _startup_port,
)


class CompilerCacheTests(unittest.TestCase):
    def test_startup_acknowledgement_admits_only_the_owned_loopback_port(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ready"
            address = b"127.0.0.1:43210"
            payload = (
                (0).to_bytes(4, "little") + len(address).to_bytes(8, "little") + address
            )
            framed = len(payload).to_bytes(4, "big") + payload
            path.write_bytes(framed)
            self.assertEqual(_startup_port(path), 43210)
            for rejected in (
                b"",
                framed[:-1],
                framed + b"extra",
                framed.replace(b"127.0.0.1", b"192.0.0.1"),
                (4).to_bytes(4, "big") + (1).to_bytes(4, "little"),
            ):
                path.write_bytes(rejected)
                self.assertIsNone(_startup_port(path))
