from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_evidence_storage``."""

import hashlib
import http.client
import ipaddress
import ssl
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from literate_ai.adapters.evidence_storage import (
    HttpsEvidenceStore,
)
from literate_ai.application.evidence_resolution import (
    ConfiguredEvidenceResolver,
    resolve_statement_evidence,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.security.evidence import (
    STATEMENT_MEDIA_TYPE,
    DerivationRun,
    Ed25519EvidenceSigner,
    EvidenceArtifact,
    EvidenceLocator,
    EvidenceNotFoundError,
    EvidenceReadLimits,
    EvidenceStatement,
    EvidenceStorageError,
    verify_evidence_statement,
)
from tests.support.fixtures_test_evidence_records import _records


def _reference(content=b"verified", media_type="application/octet-stream"):
    return BlobRef(
        hashlib.sha256(content).hexdigest(), len(content), media_type=media_type
    )


class HttpsEvidenceStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("localhost"),
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    ]
                ),
                critical=False,
            )
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True
            )
            .sign(key, hashes.SHA256())
        )
        cert, private = root / "cert.pem", root / "key.pem"
        cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        private.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        cls.client_context = ssl.create_default_context(cafile=str(cert))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, private)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_PUT(self):
                state = self.server.state
                state["requests"].append(("PUT", self.path, dict(self.headers)))
                content = self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers.get("If-None-Match") != "*":
                    self.send_response(400)
                elif self.path in state["objects"]:
                    self.send_response(412)
                else:
                    state["objects"][self.path] = (
                        content,
                        self.headers["Content-Type"],
                    )
                    self.send_response(201)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                state = self.server.state
                state["requests"].append(("GET", self.path, dict(self.headers)))
                if state["mode"] == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "/stolen")
                    self.end_headers()
                    return
                if state["mode"] == "auth":
                    self.send_response(401)
                    self.end_headers()
                    return
                if self.path not in state["objects"]:
                    self.send_response(404)
                    self.end_headers()
                    return
                content, media_type = state["objects"][self.path]
                mode = state["mode"]
                self.send_response(200)
                self.send_header(
                    "Content-Type", "text/plain" if mode == "media" else media_type
                )
                self.send_header(
                    "Content-Length", str(len(content) + (1 if mode == "size" else 0))
                )
                if mode == "duplicate":
                    self.send_header("Content-Length", str(len(content)))
                if mode == "encoding":
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                if mode == "stall":
                    state["release"].wait(timeout=2)
                if mode == "corrupt":
                    content = b"x" * len(content)
                elif mode == "truncated":
                    content = content[:-1]
                try:
                    self.wfile.write(content)
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                    pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"https://127.0.0.1:{cls.server.server_port}/evidence"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.directory.cleanup()

    def setUp(self):
        self.server.state = {
            "objects": {},
            "requests": [],
            "mode": "normal",
            "release": threading.Event(),
        }

    def store(self, **kwargs):
        return HttpsEvidenceStore(
            self.endpoint, tls_context=self.client_context, **kwargs
        )

    def test_real_tls_round_trip_and_conditional_existing_object_publication(self):
        store = self.store(writable=True, bearer_token="test-secret-canary")
        for content in (b"", b"verified", b"a" * (128 * 1024 + 7)):
            reference = store.put_bytes(content, media_type="application/octet-stream")
            self.assertEqual(store.get_bytes(reference), content)
            self.assertEqual(
                store.put_bytes(content, media_type=reference.media_type), reference
            )
        for method, path, headers in self.server.state["requests"]:
            self.assertTrue(path.startswith("/evidence/blobs/sha256/"))
            self.assertEqual(headers.get("Authorization"), "Bearer test-secret-canary")
            if method == "PUT":
                self.assertEqual(headers.get("If-None-Match"), "*")
        self.assertNotIn("test-secret-canary", repr(store))

    def test_redirect_auth_metadata_encoding_and_corruption_fail_closed(self):
        store = self.store(writable=True, bearer_token="test-secret-canary")
        reference = store.put_bytes(b"verified", media_type="application/octet-stream")
        for mode, code in (
            ("redirect", "response-refused"),
            ("auth", "authentication-refused"),
            ("media", "media-mismatch"),
            ("size", "size-mismatch"),
            ("duplicate", "size-mismatch"),
            ("encoding", "encoding-refused"),
            ("corrupt", "digest-mismatch"),
            ("truncated", "size-mismatch"),
        ):
            self.server.state["mode"] = mode
            before = len(self.server.state["requests"])
            with (
                self.subTest(mode=mode),
                self.assertRaises(EvidenceStorageError) as caught,
            ):
                store.get_bytes(reference)
            self.assertEqual(caught.exception.code, "evidence.storage." + code)
            self.assertNotIn("test-secret-canary", str(caught.exception))
            self.assertEqual(len(self.server.state["requests"]), before + 1)

    def test_read_only_bounds_missing_and_tls_verification(self):
        store = self.store()
        with self.assertRaises(EvidenceStorageError):
            store.put_bytes(b"new", media_type="text/plain")
        self.assertEqual(self.server.state["requests"], [])
        with self.assertRaises(EvidenceNotFoundError):
            store.get_bytes(_reference())
        bounded = self.store(limits=EvidenceReadLimits(4, 4, 1))
        before = len(self.server.state["requests"])
        with self.assertRaises(EvidenceStorageError):
            bounded.get_bytes(_reference())
        self.assertEqual(len(self.server.state["requests"]), before)
        untrusted = HttpsEvidenceStore(self.endpoint)
        with self.assertRaises(EvidenceStorageError):
            untrusted.get_bytes(_reference())
        self.assertEqual(len(self.server.state["requests"]), before)

    def test_invalid_configuration_never_opens_a_connection(self):
        for endpoint in (
            "http://example.test",
            "https://user:secret@example.test",
            "https://example.test/a?b=c",
            "https://example.test/#fragment",
            "https://example.test/../other",
            " https://example.test",
            "https://example.test:0",
        ):
            with (
                self.subTest(endpoint=endpoint),
                self.assertRaises(EvidenceStorageError),
            ):
                HttpsEvidenceStore(endpoint)
        weak = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        weak.check_hostname = False
        weak.verify_mode = ssl.CERT_NONE
        with self.assertRaises(EvidenceStorageError):
            HttpsEvidenceStore(self.endpoint, tls_context=weak)
        for token in ("secret\nHeader:value", "", "secret token"):
            with self.subTest(token=token), self.assertRaises(EvidenceStorageError):
                self.store(bearer_token=token)
        for timeout in (0, True, float("nan"), 301):
            with self.subTest(timeout=timeout), self.assertRaises(EvidenceStorageError):
                self.store(timeout_seconds=timeout)

    def test_signed_statement_resolves_real_remote_evidence_through_ports(self):
        writer = self.store(writable=True)
        subject = writer.put_bytes(b"source manifest", media_type="application/json")
        spec = writer.put_bytes(b"specification", media_type="text/plain")
        journal = writer.put_bytes(b"journal", media_type="application/json")
        run = DerivationRun(
            _records()[0].context,
            subject,
            (EvidenceArtifact("spec", spec),),
            journal,
            "passed",
        )
        signer = Ed25519EvidenceSigner(bytes(range(32)))
        envelope = signer.sign(STATEMENT_MEDIA_TYPE, EvidenceStatement(run).to_bytes())
        signed = verify_evidence_statement(
            envelope, trusted_public_keys=(signer.public_key,)
        )
        resolver = ConfiguredEvidenceResolver({"remote": self.store()})
        resolved = resolve_statement_evidence(
            signed.statement,
            resolver,
            locators=tuple(
                EvidenceLocator("remote", ref, 200) for ref in (subject, spec, journal)
            ),
        )
        self.assertEqual(
            {value.reference: value.content for value in resolved},
            {subject: b"source manifest", spec: b"specification", journal: b"journal"},
        )

    def test_existing_remote_corruption_is_not_overwritten_or_hidden(self):
        store = self.store(writable=True)
        reference = store.put_bytes(b"verified", media_type="application/octet-stream")
        path = next(iter(self.server.state["objects"]))
        self.server.state["objects"][path] = (b"tampered", reference.media_type)
        with self.assertRaises(EvidenceStorageError) as caught:
            store.put_bytes(b"verified", media_type=reference.media_type)
        self.assertEqual(caught.exception.code, "evidence.storage.digest-mismatch")
        self.assertEqual(self.server.state["objects"][path][0], b"tampered")

    def test_header_refusal_reads_no_body_and_stalled_body_times_out(self):
        store = self.store(writable=True)
        reference = store.put_bytes(b"verified", media_type="application/octet-stream")
        self.server.state["mode"] = "size"
        with patch("http.client.HTTPResponse.read1") as read:
            with self.assertRaises(EvidenceStorageError) as caught:
                store.get_bytes(reference)
            self.assertEqual(caught.exception.code, "evidence.storage.size-mismatch")
            read.assert_not_called()
        store = self.store(timeout_seconds=0.1)
        self.server.state["mode"] = "stall"
        try:
            with self.assertRaises(EvidenceStorageError) as caught:
                store.get_bytes(reference)
            self.assertEqual(caught.exception.code, "evidence.storage.transport-failed")
        finally:
            self.server.state["release"].set()

    def test_mutated_tls_context_cannot_disable_verification_after_configuration(self):
        context = ssl.create_default_context()
        store = HttpsEvidenceStore(self.endpoint, tls_context=context)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with self.assertRaises(EvidenceStorageError) as caught:
            store.get_bytes(_reference())
        self.assertEqual(caught.exception.code, "evidence.storage.tls-invalid")
        self.assertEqual(self.server.state["requests"], [])

    def test_rejected_response_is_closed_before_returning_to_the_caller(self):
        store = self.store(writable=True)
        reference = store.put_bytes(b"verified", media_type="application/octet-stream")
        self.server.state["mode"] = "media"
        original_close = http.client.HTTPResponse.close
        closed = []

        def close(response):
            closed.append(response)
            original_close(response)

        with patch.object(http.client.HTTPResponse, "close", close):
            try:
                store.get_bytes(reference)
            except EvidenceStorageError:
                # Check while the caller still retains the exception traceback;
                # garbage collection must not own transport-resource cleanup.
                self.assertEqual(len(closed), 1)
            else:
                self.fail("wrong media was accepted")


if __name__ == "__main__":
    unittest.main()
