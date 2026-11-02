"""Bounded source CAS reads over a real HTTP service."""

from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from literate_ai.adapters.action_blob_source import HttpActionBlobSource
from literate_ai.adapters.action_command_blob_source import CommandActionBlobSource
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.execution_dispatch import (
    LIFECYCLE_ACTION_WIRE_PROTOCOL,
    ExecutionWorker,
    ExecutionWorkerKind,
)
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from literate_ai.return_cas import CONFIG_SCHEMA
from literate_ai.storage.cas import FileSystemCAS
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

    @unittest.skipUnless(os.name == "posix", "requires bounded POSIX return reader")
    def test_real_command_streams_large_blob_and_refuses_changed_custody(self):
        """Return bytes exceed the old inline bound without becoming admission."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            store = FileSystemCAS(root / "cas")
            original = root / "large-input"
            size = 256 * 1024**2 + 17
            with original.open("xb") as stream:
                stream.seek(size - 1)
                stream.write(b"\x00")
            reference = store.put_file(original)
            configuration = root / "reader.json"
            worker = ExecutionWorker(
                "return-reader",
                ExecutionWorkerKind.COMMAND,
                command=(
                    sys.executable,
                    "-m",
                    "literate_ai.return_cas",
                    "--config",
                    str(configuration),
                ),
                action_protocol=LIFECYCLE_ACTION_WIRE_PROTOCOL,
            )
            settings = {
                "schema": CONFIG_SCHEMA,
                "cas": str(store.root),
                "worker_identity": worker.identity.uri,
                "maximum_bytes": size,
            }
            configuration.write_bytes(canonical_json_bytes(settings))
            source = CommandActionBlobSource(
                worker,
                self.deadline,
                registration_identity=canonical_identity(settings),
                cwd=root,
                environment=os.environ,
            )
            destination = root / "received"
            with destination.open("x+b") as stream:
                with self.assertRaises(TypeError):
                    source.copy_to(
                        reference,
                        stream,
                        maximum_bytes=size - 1,
                        admission_guard=lambda: None,
                    )
                self.assertEqual(stream.tell(), 0)
                self.assertEqual(
                    source.copy_to(
                        reference,
                        stream,
                        maximum_bytes=size,
                        admission_guard=lambda: None,
                    ),
                    size,
                )
            self.assertEqual(destination.stat().st_size, size)
            with destination.open("rb") as stream:
                self.assertEqual(
                    hashlib.file_digest(stream, "sha256").hexdigest(), reference.digest
                )

            content = b"returned immutable bytes\x00" * 6000
            small = store.put_bytes(content)

            class FileSink:
                # An arbitrary wrapper's write callback must never run on the
                # daemon reader, including after cancellation has returned.
                def __init__(self, stream):
                    self.stream = stream

                def fileno(self):
                    return self.stream.fileno()

                def write(self, _block):
                    raise AssertionError("arbitrary sink callback was invoked")

            with tempfile.TemporaryFile() as output:
                self.assertEqual(
                    source.copy_to(
                        small,
                        FileSink(output),
                        maximum_bytes=size,
                        admission_guard=lambda: None,
                    ),
                    small.size,
                )
                output.seek(0)
                self.assertEqual(output.read(), content)
            with tempfile.TemporaryFile() as stopped:
                with self.assertRaises(ActionWireError):
                    source.copy_to(
                        small,
                        FileSink(stopped),
                        maximum_bytes=size,
                        admission_guard=lambda: None,
                        cancelled=lambda: os.fstat(stopped.fileno()).st_size > 0,
                    )
                stopped_size = os.fstat(stopped.fileno()).st_size
                self.assertGreater(stopped_size, 0)
                self.assertLess(stopped_size, small.size)
            with self.assertRaises(TypeError):
                source.copy_to(
                    small,
                    io.BytesIO(),
                    maximum_bytes=size,
                    admission_guard=lambda: None,
                )
            with store.path_for(small).open("r+b") as stream:
                stream.write(b"!")
            with tempfile.TemporaryFile() as output:
                with self.assertRaises(ActionWireError):
                    source.copy_to(
                        small,
                        output,
                        maximum_bytes=size,
                        admission_guard=lambda: None,
                    )
            configuration.write_bytes(
                canonical_json_bytes({**settings, "maximum_bytes": 1})
            )
            with tempfile.TemporaryFile() as refused:
                with self.assertRaises(ActionWireError):
                    source.copy_to(
                        small,
                        refused,
                        maximum_bytes=size,
                        admission_guard=lambda: None,
                    )
                self.assertEqual(os.fstat(refused.fileno()).st_size, 0)

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

    def test_streamed_archive_checks_budget_short_writes_and_mid_copy_cancellation(
        self,
    ):
        content = b"retained archive bytes\x00" * 6000
        reference = BlobRef(record_identity(content).digest, len(content))

        class ShortSink(io.BytesIO):
            def write(self, block):
                return super().write(block[:1013])

        with source_cas_server({blob_path(reference): content}) as (url, requests):
            source = HttpActionBlobSource(url, self.deadline, allow_http=True)
            output = ShortSink()
            with self.assertRaises(ActionWireError):
                source.copy_to(
                    reference,
                    output,
                    maximum_bytes=reference.size - 1,
                    admission_guard=lambda: None,
                )
            self.assertEqual(requests, [])
            self.assertEqual(output.getvalue(), b"")
            self.assertEqual(
                source.copy_to(
                    reference,
                    output,
                    maximum_bytes=reference.size,
                    admission_guard=lambda: None,
                ),
                reference.size,
            )
            self.assertEqual(output.getvalue(), content)
            cancelled_output = ShortSink()
            with self.assertRaises(ActionWireError) as raised:
                source.copy_to(
                    reference,
                    cancelled_output,
                    maximum_bytes=reference.size,
                    admission_guard=lambda: None,
                    cancelled=lambda: cancelled_output.tell() > 0,
                )
            self.assertEqual(raised.exception.code, "action_source.cancelled")
            self.assertEqual(cancelled_output.tell(), 1013)

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
