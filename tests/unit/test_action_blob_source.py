"""Bounded source CAS reads over a real HTTP service."""

from __future__ import annotations

import threading
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.action_blob_source import HttpActionBlobSource
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.contracts.blobs import BlobRef


def blob_path(reference):
    return f"/blobs/sha256/{reference.digest[:2]}/{reference.digest}"


@contextmanager
def source_cas_server(blobs, *, mode="ok"):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            content = blobs.get(self.path)
            if isinstance(content, Path):
                content = content.read_bytes()
            if mode == "redirect":
                self.send_response(302)
                self.send_header("Location", "/redirected")
                self.end_headers()
                return
            if content is None:
                self.send_error(404)
                return
            self.send_response(200)
            if mode == "encoded":
                self.send_header("Content-Encoding", "gzip")
            if mode != "oversized":
                self.send_header("Content-Length", str(len(content)))
            if mode == "duplicate-length":
                self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            if mode == "corrupt":
                content = b"x" * len(content)
            elif mode == "oversized":
                content += b"x"
            elif mode == "truncated":
                content = content[:-1]
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class ActionBlobSourceTests(unittest.TestCase):
    def test_endpoint_validation_does_not_initialize_transport_or_proxy_discovery(self):
        with patch("literate_ai.adapters.action_blob_source.build_opener") as opener:
            source = HttpActionBlobSource("https://example.invalid/cas", self.deadline)
            self.assertIsNone(source.opener)
            opener.assert_not_called()

    def setUp(self):
        self.content = b"source bytes\n"
        self.reference = BlobRef(
            record_identity(self.content).digest, len(self.content)
        )
        self.deadline = ActionDispatchDeadline(
            datetime.now(UTC) + timedelta(seconds=20)
        )

    def test_exact_get_with_private_bearer_credential(self):
        with source_cas_server({blob_path(self.reference): self.content}) as (
            url,
            requests,
        ):
            client = HttpActionBlobSource(
                url, self.deadline, bearer_token="synthetic-token", allow_http=True
            )
            self.assertEqual(client.fetch(self.reference), self.content)
            self.assertEqual(
                requests, [(blob_path(self.reference), "Bearer synthetic-token")]
            )

    def test_corrupt_oversized_encoded_truncated_and_ambiguous_reads_refuse(self):
        for mode in (
            "corrupt",
            "oversized",
            "encoded",
            "truncated",
            "duplicate-length",
        ):
            with (
                self.subTest(mode=mode),
                source_cas_server(
                    {blob_path(self.reference): self.content}, mode=mode
                ) as (url, requests),
            ):
                with self.assertRaises(ActionWireError):
                    HttpActionBlobSource(url, self.deadline, allow_http=True).fetch(
                        self.reference
                    )
                self.assertEqual(len(requests), 1)

    def test_redirect_never_reaches_a_second_path(self):
        with source_cas_server({}, mode="redirect") as (url, requests):
            with self.assertRaises(ActionWireError) as raised:
                HttpActionBlobSource(
                    url, self.deadline, bearer_token="synthetic-token", allow_http=True
                ).fetch(self.reference)
            self.assertEqual(raised.exception.code, "action_source.redirect_refused")
            self.assertEqual(len(requests), 1)

    def test_missing_response_uses_sanitized_failure(self):
        with source_cas_server({}) as (url, _requests):
            with self.assertRaises(ActionWireError) as raised:
                HttpActionBlobSource(url, self.deadline, allow_http=True).fetch(
                    self.reference
                )
            self.assertEqual(raised.exception.code, "action_source.fetch_failed")
            self.assertNotIn(url, str(raised.exception))

    def test_expired_deadline_sends_no_request(self):
        expired = ActionDispatchDeadline(datetime.now(UTC) - timedelta(seconds=1))
        with source_cas_server({}) as (url, requests):
            with self.assertRaises(ActionWireError):
                HttpActionBlobSource(url, expired, allow_http=True).fetch(
                    self.reference
                )
            self.assertEqual(requests, [])

    def test_plaintext_and_embedded_credentials_require_safe_configuration(self):
        for url in (
            "http://localhost",
            "https://user:secret@localhost",
            "https://localhost/?token=secret",
            "https://localhost/#fragment",
            "file:///private/cas",
        ):
            with self.subTest(url=url), self.assertRaises(ActionWireError):
                HttpActionBlobSource(url, self.deadline)
