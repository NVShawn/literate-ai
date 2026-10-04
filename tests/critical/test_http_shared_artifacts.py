"""Real HTTP cache custody, concurrency, refusal, and transport bounds."""

from __future__ import annotations

import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from literate_ai.adapters.cache.http_artifacts import HttpSharedArtifactCache
from literate_ai.adapters.cache.shared_artifacts import (
    SharedArtifactCacheError,
)
from literate_ai.contracts.identity import canonical_json_bytes
from tests.support.fixtures_test_shared_artifact_cache import _configuration, _manifest


class HttpSharedArtifactTests(unittest.TestCase):
    def setUp(self):
        self.entries = {}
        self.requests = []
        self.redirect = False
        self.lock = threading.Lock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                fixture.requests.append(("GET", self.path, dict(self.headers)))
                if fixture.redirect:
                    self.send_response(302)
                    self.send_header("Location", "/redirected")
                    self.end_headers()
                    return
                with fixture.lock:
                    content = fixture.entries.get(self.path)
                self.send_response(404 if content is None else 200)
                self.end_headers()
                if content is not None:
                    self.wfile.write(content)

            def do_PUT(self):
                fixture.requests.append(("PUT", self.path, dict(self.headers)))
                content = self.rfile.read(int(self.headers["Content-Length"]))
                with fixture.lock:
                    if self.headers.get("If-None-Match") != "*":
                        status = 428
                    elif self.path in fixture.entries:
                        status = 412
                    else:
                        fixture.entries[self.path] = content
                        status = 201
                self.send_response(status)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.configuration = replace(
            _configuration(),
            endpoint=f"http://127.0.0.1:{self.server.server_port}/cache",
            tls_required=False,
        )
        self.cache = HttpSharedArtifactCache(self.configuration)
        self.payload = b"independently-verifiable-package\x00\xff"
        self.manifest = _manifest(self.configuration, self.payload)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_partial_publication_is_a_miss_and_corruption_is_refused(self):
        self.cache.put(self.manifest, self.payload)
        object_path = next(path for path in self.entries if "/objects/" in path)
        del self.entries[object_path]
        self.assertIsNone(self.cache.get(self.manifest))
        self.entries[object_path] = b"x" * len(self.payload)
        with self.assertRaisesRegex(SharedArtifactCacheError, "differs from manifest"):
            self.cache.get(self.manifest)
        self.entries[object_path] = self.payload * 100
        with self.assertRaisesRegex(SharedArtifactCacheError, "exceeds bound"):
            self.cache.get(self.manifest)

    def test_poisoned_manifest_and_conflicting_publication_are_refused(self):
        self.cache.put(self.manifest, self.payload)
        other = replace(self.manifest, mode=0o777)
        manifest_path = next(path for path in self.entries if "/manifests/" in path)
        self.entries[manifest_path] = canonical_json_bytes(other.to_dict())
        with self.assertRaisesRegex(SharedArtifactCacheError, "differs from authority"):
            self.cache.get(self.manifest)
        with self.assertRaisesRegex(
            SharedArtifactCacheError, "published cache entry differs"
        ):
            self.cache.put(self.manifest, self.payload)
        self.assertEqual(
            self.entries[manifest_path], canonical_json_bytes(other.to_dict())
        )

    def test_redirect_does_not_forward_credentials(self):
        self.redirect = True
        configuration = replace(
            self.configuration, credential_reference="env:TEST_TOKEN"
        )
        cache = HttpSharedArtifactCache(
            configuration, credential_resolver=lambda _: "secret-token"
        )
        with self.assertRaisesRegex(
            SharedArtifactCacheError, "HTTP request failed"
        ) as caught:
            cache.get(_manifest(configuration, self.payload))
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertEqual(len(self.requests), 1)
