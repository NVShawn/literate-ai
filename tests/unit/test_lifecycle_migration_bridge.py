"""Typed, release-bound lifecycle migration bridge tests."""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from literate_ai.application import (
    LifecycleBridge,
    LifecycleBridgeError,
    LifecyclePortResult,
)
from literate_ai.contracts import (
    LifecycleRequest,
    LifecycleSeam,
    canonical_identity,
    framework_release_identity,
    wire_digest,
)


@dataclass
class EchoPort:
    seam: LifecycleSeam
    wrote: bool = False
    calls: int = 0

    def invoke(self, request, input_bytes, input_payload):
        self.calls += 1
        return LifecyclePortResult(
            output_bytes=input_bytes,
            output_payload=dict(input_payload),
            wrote=self.wrote,
        )


class LifecycleMigrationBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.release = framework_release_identity("0.1.1a1")
        self.port = EchoPort(LifecycleSeam.SETTINGS)
        self.bridge = LifecycleBridge(self.release, {LifecycleSeam.SETTINGS: self.port})
        self.input_bytes = b'{  "enabled" : true }\n'
        self.payload = {"enabled": True}

    def request(self, *, read_only: bool = True) -> LifecycleRequest:
        return LifecycleRequest(
            request_id="settings-read-001",
            seam=LifecycleSeam.SETTINGS,
            operation="snapshot",
            input_wire_digest=wire_digest(self.input_bytes),
            input_payload_identity=canonical_identity(self.payload),
            framework_release_identity=self.release.identity,
            read_only=read_only,
        )

    def test_exact_release_delegates_through_typed_contract(self) -> None:
        execution = self.bridge.execute(
            self.request(),
            input_bytes=self.input_bytes,
            input_payload=self.payload,
            expected_release_identity=self.release.identity.uri,
        )

        self.assertEqual(execution.output_bytes, self.input_bytes)
        self.assertEqual(
            execution.result.framework_release_identity, self.release.identity
        )
        self.assertEqual(
            execution.result.output_wire_digest, wire_digest(self.input_bytes)
        )
        self.assertEqual(self.port.calls, 1)

    def test_release_mismatch_fails_before_port_invocation(self) -> None:
        with self.assertRaises(LifecycleBridgeError) as caught:
            self.bridge.execute(
                self.request(),
                input_bytes=self.input_bytes,
                input_payload=self.payload,
                expected_release_identity="sha256:" + "0" * 64,
            )

        self.assertEqual(caught.exception.code, "migration.release-identity-mismatch")
        self.assertEqual(self.port.calls, 0)

    def test_shadow_write_attempt_fails_closed(self) -> None:
        self.port.wrote = True

        with self.assertRaises(LifecycleBridgeError) as caught:
            self.bridge.execute(
                self.request(read_only=True),
                input_bytes=self.input_bytes,
                input_payload=self.payload,
                expected_release_identity=self.release.identity.uri,
            )

        self.assertEqual(caught.exception.code, "migration.shadow-write-attempt")

    def test_input_wire_drift_fails_before_port_invocation(self) -> None:
        with self.assertRaises(LifecycleBridgeError) as caught:
            self.bridge.execute(
                self.request(),
                input_bytes=self.input_bytes + b" ",
                input_payload=self.payload,
                expected_release_identity=self.release.identity.uri,
            )

        self.assertEqual(caught.exception.code, "migration.input-wire-mismatch")
        self.assertEqual(self.port.calls, 0)


if __name__ == "__main__":
    unittest.main()
