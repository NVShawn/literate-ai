"""Authenticated inherited-session provider protocol tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

from literate_ai.adapters.models import (
    DirectoryInheritedSessionCoordinator,
    DirectoryInheritedSessionTransport,
    InheritedSessionDelivery,
    InheritedSessionError,
    InheritedSessionProviderAdapter,
    InheritedSessionProviderConfig,
)
from literate_ai.contracts import (
    ContractValidationError,
    InheritedSessionContextBundle,
    InheritedSessionHandoffEvidence,
    InheritedSessionOutcome,
    InheritedSessionRequest,
    InheritedSessionResponse,
    canonical_identity,
    canonical_json_bytes,
)

SECRET = b"a" * 32


def identity(label: str):
    return canonical_identity({"inherited-session-test": label})


def sign(payload: bytes, secret: bytes = SECRET, *, domain: bytes = b"\x00") -> str:
    return hmac.new(secret, domain + payload, hashlib.sha256).hexdigest()


def context_bundle() -> InheritedSessionContextBundle:
    prompt = b"Generate exact source beneath source/."
    return InheritedSessionContextBundle(
        bounded_prompt=prompt,
        workspace_locator="/tmp/inherited-session-workspace",
        context_manifest_identity=identity("context"),
        prompt_identity=identity_from_bytes(prompt),
        writable_boundary_identity=identity("workspace"),
        component_lock_identity=identity("lock"),
        recipe_identity=identity("recipe"),
        model_name="test-model",
        model_binding_identity=identity("model-binding"),
        output_contract_identity=identity("output-contract"),
    )


def request_auth_payload(request_json: bytes, bundle_json: bytes) -> bytes:
    import base64

    return canonical_json_bytes(
        {
            "request": base64.b64encode(request_json).decode("ascii"),
            "context_bundle": base64.b64encode(bundle_json).decode("ascii"),
        }
    )


def config() -> InheritedSessionProviderConfig:
    return InheritedSessionProviderConfig(
        identity("provider"),
        identity("session"),
        "ephemeral-test-key",
        30,
    )


class Transport:
    def __init__(
        self,
        *,
        outcome=InheritedSessionOutcome.SUCCEEDED,
        response_provider=None,
        response_session=None,
        response_request=None,
        tag_secret=SECRET,
        payload=b"candidate source archive",
        raises_timeout=False,
    ):
        self.outcome = outcome
        self.response_provider = response_provider
        self.response_session = response_session
        self.response_request = response_request
        self.tag_secret = tag_secret
        self.payload = payload
        self.raises_timeout = raises_timeout
        self.delivery = None

    def exchange(
        self,
        request_json,
        context_bundle_json,
        request_authentication_tag,
        *,
        timeout_seconds,
        cancelled,
    ):
        self.request_json = request_json
        self.context_bundle_json = context_bundle_json
        self.request_authentication_tag = request_authentication_tag
        self.timeout_seconds = timeout_seconds
        self.cancelled = cancelled
        if self.raises_timeout:
            raise TimeoutError
        self.assert_request_authenticated = hmac.compare_digest(
            sign(request_auth_payload(request_json, context_bundle_json)),
            request_authentication_tag,
        )
        request = InheritedSessionRequest.from_dict(json.loads(request_json))
        if self.outcome is InheritedSessionOutcome.SUCCEEDED:
            response = InheritedSessionResponse(
                self.response_request or request.identity,
                self.response_provider or request.provider_identity,
                self.response_session or request.session_identity,
                1,
                self.outcome,
                identity("source-tree"),
                identity_from_bytes(self.payload),
                identity("transcript"),
                identity("provider-evidence"),
            )
            payload = self.payload
        else:
            response = InheritedSessionResponse(
                request.identity,
                request.provider_identity,
                request.session_identity,
                1,
                self.outcome,
                reason_code=f"inherited_session.{self.outcome.value}",
            )
            payload = None
        response_json = canonical_json_bytes(response.to_dict())
        self.delivery = InheritedSessionDelivery(
            response_json,
            payload,
            sign(response_json, self.tag_secret, domain=b"\x01"),
        )
        return self.delivery


def identity_from_bytes(value: bytes):
    from literate_ai.contracts import ContentIdentity

    return ContentIdentity.parse_uri("sha256:" + hashlib.sha256(value).hexdigest())


def execute(adapter):
    return adapter.execute(
        generation_request_identity=identity("request"),
        generation_plan_identity=identity("plan"),
        context_bundle=context_bundle(),
    )


class InheritedSessionProviderTests(unittest.TestCase):
    def adapter(self, transport, *, clock=lambda: datetime(2026, 8, 15, tzinfo=UTC)):
        return InheritedSessionProviderAdapter(
            config(),
            transport,
            secret_provider=lambda key_id: SECRET,
            clock=clock,
        )

    def test_success_authenticates_without_nested_process_or_private_prompt(self):
        transport = Transport()
        result = execute(self.adapter(transport))

        self.assertTrue(transport.assert_request_authenticated)
        self.assertEqual(result.source_payload, transport.payload)
        self.assertEqual(
            result.evidence.response.outcome,
            InheritedSessionOutcome.SUCCEEDED,
        )
        public = result.evidence.to_dict()
        self.assertNotIn("prompt_content", json.dumps(public))
        self.assertNotIn(SECRET.decode(), json.dumps(public))
        self.assertEqual(
            InheritedSessionHandoffEvidence.from_dict(public),
            result.evidence,
        )

    def test_authentication_and_session_mismatch_fail_closed(self):
        for transport, code in (
            (
                Transport(tag_secret=b"b" * 32),
                "inherited_session.authentication_failed",
            ),
            (
                Transport(response_session=identity("other-session")),
                "inherited_session.response_binding_mismatch",
            ),
        ):
            with self.subTest(code=code):
                with self.assertRaises(InheritedSessionError) as raised:
                    execute(self.adapter(transport))
                self.assertEqual(raised.exception.code, code)

    def test_source_payload_tamper_fails_closed(self):
        transport = Transport()
        original_exchange = transport.exchange

        def tampered(*args, **kwargs):
            delivery = original_exchange(*args, **kwargs)
            return InheritedSessionDelivery(
                delivery.response_json,
                b"tampered",
                delivery.authentication_tag,
            )

        transport.exchange = tampered
        with self.assertRaises(InheritedSessionError) as raised:
            execute(self.adapter(transport))
        self.assertEqual(raised.exception.code, "inherited_session.source_tampered")

        valid_transport = Transport()
        evidence = execute(self.adapter(valid_transport)).evidence.to_dict()
        evidence["response"]["provider_evidence_identity"] = identity(
            "tampered-evidence"
        ).to_dict()
        with self.assertRaisesRegex(ContractValidationError, "evidence_identity"):
            InheritedSessionHandoffEvidence.from_dict(evidence)

    def test_directory_protocol_fails_closed_on_replay_and_disconnect(self):
        with tempfile.TemporaryDirectory() as temporary:
            channel = Path(temporary)
            channel.chmod(0o700)
            transport = DirectoryInheritedSessionTransport(channel)
            failure = []

            def run():
                try:
                    execute(self.adapter(transport))
                except Exception as exc:
                    failure.append(exc)

            worker = threading.Thread(target=run)
            worker.start()
            request_path = (
                transport.directory / DirectoryInheritedSessionTransport.REQUEST_NAME
            )
            for _ in range(200):
                if request_path.exists():
                    break
                time.sleep(0.005)
            coordinator = DirectoryInheritedSessionCoordinator(
                transport.directory, config(), secret=SECRET
            )
            coordinator.receive()
            with self.assertRaises(InheritedSessionError) as replay:
                coordinator.receive()
            self.assertEqual(
                replay.exception.code, "inherited_session.duplicate_request"
            )
            self.assertEqual(
                DirectoryInheritedSessionCoordinator.discover(channel),
                (transport.directory,),
            )
            coordinator.disconnect()
            worker.join(timeout=5)
            self.assertEqual(failure[0].code, "inherited_session.disconnected")


if __name__ == "__main__":
    unittest.main()
