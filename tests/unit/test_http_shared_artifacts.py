"""Real HTTP cache custody, concurrency, refusal, and transport bounds."""

from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from literate_ai.adapters.cache.http_artifacts import HttpSharedArtifactCache
from literate_ai.adapters.cache.layered_artifacts import LayeredSharedArtifactCache
from literate_ai.adapters.cache.shared_artifacts import (
    LocalSharedArtifactCache,
    SharedArtifactCacheError,
)
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.shared_cache import SharedCacheAccessMode
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

    def test_local_quota_does_not_prevent_verified_remote_reuse_or_publication(self):
        configuration = replace(self.configuration, maximum_bytes=len(self.payload))
        remote = HttpSharedArtifactCache(configuration)
        with tempfile.TemporaryDirectory() as directory:
            local = LocalSharedArtifactCache(configuration, Path(directory).resolve())
            cache = LayeredSharedArtifactCache(local, remote)
            self.assertTrue(cache.put(self.manifest, self.payload))
            self.assertIsNone(local.get(self.manifest))
            result = cache.lookup(self.manifest)
            self.assertEqual(result.payload, self.payload)
            self.assertEqual(result.source, "remote")
            self.assertEqual(result.unavailable_code, "shared_cache.quota_exhausted")
            self.assertIsNone(local.get(self.manifest))

    def test_cold_then_concurrent_publication_then_warm(self):
        self.assertIsNone(self.cache.get(self.manifest))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(
                pool.map(
                    lambda _: self.cache.put(self.manifest, self.payload), range(8)
                )
            )
        self.assertEqual(self.cache.get(self.manifest), self.payload)
        self.assertEqual(len(self.entries), 2)
        self.assertTrue(
            all(
                headers["If-None-Match"] == "*"
                for method, _, headers in self.requests
                if method == "PUT"
            )
        )

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

    def test_read_only_refuses_publication_before_network(self):
        configuration = replace(
            self.configuration,
            policies=_configuration(SharedCacheAccessMode.READ_ONLY).policies,
        )
        cache = HttpSharedArtifactCache(configuration)
        with self.assertRaisesRegex(SharedArtifactCacheError, "does not authorize"):
            cache.put(_manifest(configuration, self.payload), self.payload)
        self.assertEqual(self.requests, [])

    def test_read_only_consumer_reads_writer_entry(self):
        self.cache.put(self.manifest, self.payload)
        configuration = replace(
            self.configuration,
            policies=_configuration(SharedCacheAccessMode.READ_ONLY).policies,
            local_root_reference="other-local-root",
        )
        self.assertNotEqual(configuration.identity, self.configuration.identity)
        self.assertEqual(_manifest(configuration, self.payload), self.manifest)
        reader = HttpSharedArtifactCache(configuration)
        self.assertEqual(
            reader.get(_manifest(configuration, self.payload)), self.payload
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

    def test_invalid_payload_and_oversized_authority_never_reach_network(self):
        with self.assertRaisesRegex(SharedArtifactCacheError, "payload differs"):
            self.cache.put(self.manifest, b"wrong")
        oversized = replace(
            self.manifest, size_bytes=self.configuration.maximum_bytes + 1
        )
        with self.assertRaisesRegex(SharedArtifactCacheError, "size policy"):
            self.cache.get(oversized)
        self.assertEqual(self.requests, [])

    def test_remote_hit_populates_local_and_unavailable_cache_is_optional(self):
        self.cache.put(self.manifest, self.payload)
        with tempfile.TemporaryDirectory() as directory:
            local = LocalSharedArtifactCache(
                self.configuration, Path(directory).resolve()
            )
            cache = LayeredSharedArtifactCache(local, self.cache)
            result = cache.lookup(self.manifest)
            self.assertEqual((result.source, result.payload), ("remote", self.payload))
            request_count = len(self.requests)
            self.assertEqual(cache.lookup(self.manifest).source, "local")
            self.assertEqual(len(self.requests), request_count)
        self.server.shutdown()
        self.server.server_close()
        with tempfile.TemporaryDirectory() as directory:
            local = LocalSharedArtifactCache(
                self.configuration, Path(directory).resolve()
            )
            cache = LayeredSharedArtifactCache(local, self.cache)
            result = cache.lookup(self.manifest)
            self.assertEqual(result.source, "miss")
            self.assertEqual(result.unavailable_code, "shared_cache.unavailable")
            self.assertFalse(cache.put(self.manifest, self.payload))
            self.assertEqual(cache.get(self.manifest), self.payload)
