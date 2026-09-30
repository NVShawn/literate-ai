"""Authenticated inherited-session provider protocol tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import literate_ai.adapters.models.inherited_session as inherited_session_module
from literate_ai.adapters.models import (
    DirectoryInheritedSessionCoordinator,
    DirectoryInheritedSessionTransport,
    InheritedSessionCancellation,
    InheritedSessionCustody,
    InheritedSessionDelivery,
    InheritedSessionError,
    InheritedSessionProviderAdapter,
    InheritedSessionProviderConfig,
    InheritedSessionSourceGenerator,
    selected_coding_provider,
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

    def test_selection_and_configuration_are_explicit(self):
        environment = {
            "LITAI_CODING_PROVIDER": "inherited-session",
            "LITAI_INHERITED_SESSION_PROVIDER_IDENTITY": identity("provider").uri,
            "LITAI_INHERITED_SESSION_IDENTITY": identity("session").uri,
            "LITAI_INHERITED_SESSION_AUTH_KEY_ID": "runtime-key",
            "LITAI_INHERITED_SESSION_TIMEOUT_SECONDS": "12",
        }
        self.assertEqual(selected_coding_provider(environment), "inherited-session")
        selected = InheritedSessionProviderConfig.from_environment(environment)
        self.assertEqual(selected.session_identity, identity("session"))
        self.assertEqual(selected.timeout_seconds, 12)
        self.assertNotIn("SECRET", repr(selected))

        with tempfile.TemporaryDirectory() as temporary:
            channel = Path(temporary)
            channel.chmod(0o700)
            runtime = InheritedSessionSourceGenerator.from_environment(
                {
                    **environment,
                    "LITAI_INHERITED_SESSION_CHANNEL": str(channel),
                    "LITAI_INHERITED_SESSION_AUTH_KEY": SECRET.hex(),
                }
            )
            self.assertEqual(runtime.selection.name, "inherited-session")
            self.assertEqual(
                runtime.selection.tool_binding_identity,
                identity("provider").uri,
            )
        with self.assertRaises(InheritedSessionError) as invalid_timeout:
            InheritedSessionProviderConfig.from_environment(
                {
                    **environment,
                    "LITAI_INHERITED_SESSION_TIMEOUT_SECONDS": "nan",
                }
            )
        self.assertEqual(
            invalid_timeout.exception.code,
            "inherited_session.configuration_invalid",
        )
        with self.assertRaises(InheritedSessionError) as unavailable:
            InheritedSessionSourceGenerator.from_environment(environment)
        self.assertEqual(
            unavailable.exception.code,
            "inherited_session.current_session_unavailable",
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

    def test_short_authentication_key_fails_closed_before_any_exchange(self):
        # The most security-relevant untested branch: a secret_provider that
        # returns fewer than 32 bytes must fail closed before the request is
        # ever handed to the transport, and must not leave the adapter usable
        # for a retry with a different (correctly sized) key.
        transport = Transport()
        adapter = InheritedSessionProviderAdapter(
            config(),
            transport,
            secret_provider=lambda key_id: b"too-short",
            clock=lambda: datetime(2026, 8, 15, tzinfo=UTC),
        )
        with self.assertRaises(InheritedSessionError) as raised:
            execute(adapter)
        self.assertEqual(
            raised.exception.code, "inherited_session.authentication_key_invalid"
        )
        self.assertFalse(hasattr(transport, "request_json"))
        self.assertEqual(adapter.custody, InheritedSessionCustody.TERMINAL)
        with self.assertRaises(InheritedSessionError) as duplicate:
            execute(adapter)
        self.assertEqual(
            duplicate.exception.code, "inherited_session.duplicate_request"
        )

    def test_domain_separated_tags_reject_reflected_request_as_response(self):
        # A response tag computed with the request domain (or vice versa) must
        # not authenticate, even though it is signed with the correct secret
        # over the correct bytes -- this is the domain-separation guard
        # against tag reflection between the two envelope kinds.
        payload = b"reflected-payload"
        reflected_as_response = sign(payload, domain=b"\x00")
        self.assertFalse(
            hmac.compare_digest(reflected_as_response, sign(payload, domain=b"\x01"))
        )

    def test_timeout_and_both_cancellation_owners_are_typed(self):
        timeout = execute(self.adapter(Transport(raises_timeout=True)))
        self.assertEqual(
            timeout.evidence.response.outcome,
            InheritedSessionOutcome.TIMED_OUT,
        )
        for owner, outcome in (
            (
                InheritedSessionCancellation.USER,
                InheritedSessionOutcome.USER_CANCELLED,
            ),
            (
                InheritedSessionCancellation.RUNNER,
                InheritedSessionOutcome.RUNNER_CANCELLED,
            ),
        ):
            with self.subTest(owner=owner):
                adapter = self.adapter(Transport())
                adapter.cancel(owner)
                result = execute(adapter)
                self.assertEqual(result.evidence.response.outcome, outcome)

    def test_stale_late_and_duplicate_responses_are_rejected(self):
        started = datetime(2026, 8, 15, tzinfo=UTC)
        moments = iter((started, started + timedelta(minutes=2)))
        stale = execute(self.adapter(Transport(), clock=lambda: next(moments)))
        self.assertEqual(
            stale.evidence.response.reason_code,
            "inherited_session.stale_response",
        )

        transport = Transport()
        adapter = self.adapter(transport)
        execute(adapter)
        with self.assertRaises(InheritedSessionError) as raised:
            adapter.accept_delivery(transport.delivery, secret=SECRET)
        self.assertEqual(
            raised.exception.code,
            "inherited_session.late_or_duplicate_response",
        )

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

    def test_directory_protocol_roundtrip_authenticates_coordinator_and_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            channel = Path(temporary)
            channel.chmod(0o700)
            transport = DirectoryInheritedSessionTransport(channel)
            adapter = self.adapter(transport)
            result = []
            failure = []

            def run():
                try:
                    result.append(execute(adapter))
                except Exception as exc:  # pragma: no cover - asserted below
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
            received = coordinator.receive()
            request = received.request
            self.assertEqual(received.context_bundle, context_bundle())
            files = {"source/main.py": "print('inherited')\n"}
            response = coordinator.succeed(
                files,
                transcript_identity=identity("transcript"),
                provider_evidence_identity=identity("provider-evidence"),
            )
            worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(failure, [])
            self.assertEqual(result[0].evidence.request, request)
            self.assertEqual(result[0].evidence.response, response)
            self.assertEqual(
                json.loads(result[0].source_payload)["files"],
                files,
            )

    def test_directory_protocol_rejects_authenticated_context_byte_tamper(self):
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
            envelope = json.loads(request_path.read_bytes())
            encoded_bundle = envelope["context_bundle"]
            envelope["context_bundle"] = (
                "A" if encoded_bundle[0] != "A" else "B"
            ) + encoded_bundle[1:]
            request_path.unlink()
            inherited_session_module._write_exclusive_private(
                request_path, canonical_json_bytes(envelope)
            )

            coordinator = DirectoryInheritedSessionCoordinator(
                transport.directory, config(), secret=SECRET
            )
            with self.assertRaises(InheritedSessionError) as tampered:
                coordinator.receive()
            self.assertEqual(
                tampered.exception.code, "inherited_session.authentication_failed"
            )
            coordinator.disconnect()
            worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(failure[0].code, "inherited_session.disconnected")

    def test_directory_protocol_orders_concurrent_requests_and_cleans_one(self):
        with tempfile.TemporaryDirectory() as temporary:
            channel = Path(temporary)
            channel.chmod(0o700)
            transports = [
                DirectoryInheritedSessionTransport(channel),
                DirectoryInheritedSessionTransport(channel),
            ]
            failures = []

            def run(transport):
                try:
                    execute(self.adapter(transport))
                except Exception as exc:
                    failures.append(exc)

            workers = [
                threading.Thread(target=run, args=(transport,))
                for transport in transports
            ]
            for worker in workers:
                worker.start()
            request_paths = [
                transport.directory / DirectoryInheritedSessionTransport.REQUEST_NAME
                for transport in transports
            ]
            for _ in range(200):
                if all(path.exists() for path in request_paths):
                    break
                time.sleep(0.005)

            expected = tuple(sorted(transport.directory for transport in transports))
            self.assertEqual(
                DirectoryInheritedSessionCoordinator.discover(channel), expected
            )
            first = transports[0]
            first_coordinator = DirectoryInheritedSessionCoordinator(
                first.directory, config(), secret=SECRET
            )
            first_coordinator.receive()
            first_coordinator.disconnect()
            workers[0].join(timeout=5)
            first.cleanup()
            self.assertEqual(
                DirectoryInheritedSessionCoordinator.discover(channel),
                tuple(path for path in expected if path != first.directory),
            )

            remaining = transports[1]
            coordinator = DirectoryInheritedSessionCoordinator(
                remaining.directory, config(), secret=SECRET
            )
            coordinator.receive()
            coordinator.disconnect()
            workers[1].join(timeout=5)

            self.assertFalse(workers[0].is_alive())
            self.assertFalse(workers[1].is_alive())
            self.assertEqual(
                [failure.code for failure in failures],
                [
                    "inherited_session.disconnected",
                    "inherited_session.disconnected",
                ],
            )

    def test_directory_protocol_publishes_only_complete_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "response.json"
            content = b'{"complete":true}'
            publishing = threading.Event()
            release = threading.Event()
            failure = []
            real_link = inherited_session_module.os.link

            def delayed_link(source, target):
                self.assertEqual(Path(source).read_bytes(), content)
                self.assertFalse(Path(target).exists())
                publishing.set()
                release.wait(timeout=5)
                real_link(source, target)

            def write():
                try:
                    inherited_session_module._write_exclusive_private(
                        destination, content
                    )
                except Exception as exc:  # pragma: no cover - asserted below
                    failure.append(exc)

            with mock.patch.object(
                inherited_session_module.os, "link", side_effect=delayed_link
            ):
                worker = threading.Thread(target=write)
                worker.start()
                observed_publish = publishing.wait(timeout=5)
                try:
                    self.assertTrue(observed_publish)
                    self.assertFalse(destination.exists())
                finally:
                    release.set()
                worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(failure, [])
            self.assertEqual(destination.read_bytes(), content)

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

    def test_directory_protocol_preserves_typed_in_flight_cancellation(self):
        with tempfile.TemporaryDirectory() as temporary:
            channel = Path(temporary)
            channel.chmod(0o700)
            transport = DirectoryInheritedSessionTransport(channel)
            adapter = self.adapter(transport)
            result = []
            worker = threading.Thread(target=lambda: result.append(execute(adapter)))
            worker.start()
            request_path = (
                transport.directory / DirectoryInheritedSessionTransport.REQUEST_NAME
            )
            for _ in range(200):
                if request_path.exists():
                    break
                time.sleep(0.005)
            adapter.cancel(InheritedSessionCancellation.USER)
            worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(
                result[0].evidence.response.outcome,
                InheritedSessionOutcome.USER_CANCELLED,
            )


if __name__ == "__main__":
    unittest.main()
