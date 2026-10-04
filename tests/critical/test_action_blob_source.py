"""Bounded source CAS reads over a real HTTP service."""

from __future__ import annotations

import threading
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from literate_ai.adapters.action_blob_source import HttpActionBlobSource
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.contracts.blobs import BlobRef
from tests.support.action_deadline import ACTION_TEST_DEADLINE


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
    def setUp(self):
        self.content = b"source bytes\n"
        self.reference = BlobRef(
            record_identity(self.content).digest, len(self.content)
        )
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + ACTION_TEST_DEADLINE)

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

    def test_redirect_never_reaches_a_second_path(self):
        with source_cas_server({}, mode="redirect") as (url, requests):
            with self.assertRaises(ActionWireError) as raised:
                HttpActionBlobSource(
                    url, self.deadline, bearer_token="synthetic-token", allow_http=True
                ).fetch(self.reference)
            self.assertEqual(raised.exception.code, "action_source.redirect_refused")
            self.assertEqual(len(requests), 1)

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
