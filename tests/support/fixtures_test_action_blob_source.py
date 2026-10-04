from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_action_blob_source``."""

import threading


from contextlib import contextmanager


from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pathlib import Path





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

