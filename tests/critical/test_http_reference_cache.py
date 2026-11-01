"""Real HTTP custody, publication limits and read/write policy for the reference."""

from __future__ import annotations

import concurrent.futures
import os
import socket
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from literate_ai.adapters.cache.http_artifacts import HttpSharedArtifactCache
from literate_ai.adapters.cache.http_reference import ReferenceSharedCacheServer
from literate_ai.adapters.cache.shared_artifacts import SharedArtifactCacheError
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from literate_ai.contracts.shared_cache import SharedCacheAccessMode
from tests.support.fixtures_test_shared_artifact_cache import _configuration, _manifest

_READER = "synthetic-reader-token-000"
_WRITER = "synthetic-writer-token-000"


class ReferenceHttpCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.server = ReferenceSharedCacheServer(
            _configuration(),
            self.root,
            reader_token=_READER,
            writer_token=_WRITER,
            pending_bytes=8192,
            maximum_object_bytes=4096,
            deadline_seconds=2,
        )
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"
        self.configuration = replace(
            _configuration(),
            endpoint=self.endpoint,
            tls_required=False,
            credential_reference="private-reference",
        )
        self.writer = self.client(_WRITER)
        self.reader = self.client(_READER, read_only=True)
        self.payload = b"verified-native-package\x00\xff"
        self.manifest = _manifest(self.configuration, self.payload)

    def client(self, token, *, read_only=False):
        configuration = self.configuration
        if read_only:
            configuration = replace(
                configuration,
                policies=tuple(
                    replace(policy, mode=SharedCacheAccessMode.READ_ONLY)
                    for policy in configuration.policies
                ),
            )
        return HttpSharedArtifactCache(
            configuration, credential_resolver=lambda _: token
        )

    def path(self, manifest=None, *, object=False):
        manifest = manifest or self.manifest
        leaf = (
            f"objects/{manifest.payload_identity.digest}"
            if object
            else f"manifests/{manifest.key_identity.digest}.json"
        )
        return f"/release/{manifest.namespace.value}/{leaf}"

    def request(self, path, body=None, *, token=_WRITER, headers=None):
        fields = {"Authorization": "Bearer " + token, "If-None-Match": "*"}
        fields.update(headers or {})
        request = Request(
            self.endpoint + path,
            data=body,
            headers=fields,
            method="GET" if body is None else "PUT",
        )
        try:
            with urlopen(request, timeout=4) as response:
                return response.status, response.read()
        except HTTPError as error:
            error.close()
            return error.code, b""

    def test_cold_warm_readonly_and_concurrent_identical_writers(self):
        self.assertIsNone(self.reader.get(self.manifest))
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(
                pool.map(
                    lambda _: self.writer.put(self.manifest, self.payload), range(2)
                )
            )
        self.assertEqual(self.reader.get(self.manifest), self.payload)
        self.assertFalse(self.server.pending)
        with self.assertRaisesRegex(SharedArtifactCacheError, "does not authorize"):
            self.reader.put(self.manifest, self.payload)
        self.assertEqual(
            self.request(self.path(object=True), self.payload, token=_READER)[0], 403
        )
        self.assertEqual(self.request(self.path(), token="wrong-private-token")[0], 401)

    def test_conflicts_corruption_and_payload_before_manifest(self):
        encoded = canonical_json_bytes(self.manifest.to_dict())
        self.assertEqual(self.request(self.path(), encoded)[0], 409)
        self.writer.put(self.manifest, self.payload)
        changed = replace(self.manifest, mode=0o777)
        self.assertEqual(
            self.request(self.path(), canonical_json_bytes(changed.to_dict()))[0], 409
        )
        self.assertEqual(self.reader.get(self.manifest), self.payload)
        path = (
            self.server.cache.root
            / "package"
            / "objects"
            / self.manifest.payload_identity.digest
        )
        path.write_bytes(b"corrupt")
        with self.assertRaises(SharedArtifactCacheError):
            self.reader.get(self.manifest)

    def test_paths_framing_namespace_and_conditional_create(self):
        for path in (
            "/release/package/objects/../secret",
            "/release/package/objects/%2e%2e",
            self.path() + "?secret=value",
            self.path().replace("package", "compiler"),
            "/release/../package/objects/" + "0" * 64,
        ):
            self.assertNotEqual(self.request(path)[0], 200)
        self.assertEqual(
            self.request(
                self.path(object=True), self.payload, headers={"If-None-Match": "wrong"}
            )[0],
            428,
        )
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        connection.putrequest("PUT", self.path(object=True))
        for name, value in (
            ("Authorization", "Bearer " + _WRITER),
            ("If-None-Match", "*"),
            ("Content-Length", "1"),
            ("Content-Length", "1"),
        ):
            connection.putheader(name, value)
        connection.endheaders(b"x")
        self.assertEqual(connection.getresponse().status, 400)
        connection.close()
        self.assertEqual(self.request(self.path(object=True), b"x")[0], 422)
        self.assertEqual(self.request(self.path(object=True), b"x" * 4097)[0], 413)

    def test_pending_quota_expiry_and_interrupted_upload(self):
        # Pending publication never enters committed disk storage.
        self.server.pending_limit = len(self.payload)
        self.assertEqual(self.request(self.path(object=True), self.payload)[0], 201)
        other_payload = b"different"
        other = _manifest(self.configuration, other_payload)
        self.assertEqual(
            self.request(self.path(other, object=True), other_payload)[0], 507
        )
        key = (self.manifest.namespace, self.manifest.payload_identity.digest)
        content, _ = self.server.pending[key]
        self.server.pending[key] = (content, time.monotonic() - 100)
        self.assertEqual(self.request(self.path(object=True))[0], 404)
        self.assertFalse(self.server.pending)
        self.assertFalse((self.server.cache.root / "package").exists())
        sock = socket.create_connection(("127.0.0.1", self.server.server_port))
        sock.sendall(
            (
                f"PUT {self.path(object=True)} HTTP/1.0\r\n"
                f"Authorization: Bearer {_WRITER}\r\n"
                "If-None-Match: *\r\nContent-Length: 100\r\n\r\npartial"
            ).encode()
        )
        sock.shutdown(socket.SHUT_WR)
        while sock.recv(4096):
            pass
        sock.close()
        self.assertEqual(self.request(self.path(object=True))[0], 404)

    def test_committed_quota_and_retention_use_existing_local_owner(self):
        # Just one complete entry fits. Publishing a second evicts the first.
        capacity = len(self.payload) + len(
            canonical_json_bytes(self.manifest.to_dict())
        )
        self.server.cache.configuration = replace(
            _configuration(), maximum_bytes=capacity
        )
        self.writer.put(self.manifest, self.payload)
        other = replace(
            self.manifest, key_identity=canonical_identity("another-action")
        )
        self.writer.put(other, self.payload)
        self.assertIsNone(self.reader.get(self.manifest))
        self.assertEqual(self.reader.get(other), self.payload)
        path = (
            self.server.cache.root
            / "package"
            / "manifests"
            / f"{other.key_identity.digest}.json"
        )
        os.utime(path, (0, 0))
        self.assertIsNone(self.reader.get(other))
        self.writer.put(other, self.payload)
        self.assertEqual(self.reader.get(other), self.payload)

    def test_total_socket_deadline_releases_partial_header_connection(self):
        sock = socket.create_connection(("127.0.0.1", self.server.server_port))
        sock.settimeout(4)
        sock.sendall(b"GET /")
        self.assertEqual(sock.recv(4096), b"")
        sock.close()
        self.writer.put(self.manifest, self.payload)
        self.assertEqual(self.reader.get(self.manifest), self.payload)

    def test_private_bounds_readonly_service_and_hardlink_refusal(self):
        for overrides in (
            {"pending_bytes": True},
            {"maximum_connections": 33},
            {"deadline_seconds": 0},
            {"pending_seconds": 3601},
            {"reader_token": None},
            {"writer_token": _READER},
            {"reader_token": "header\r\ninjection"},
            {"pending_bytes": 1, "maximum_object_bytes": 2},
        ):
            options = {"reader_token": _READER, "writer_token": _WRITER}
            options.update(overrides)
            with self.assertRaises(ValueError):
                ReferenceSharedCacheServer(_configuration(), self.root, **options)
        self.writer.put(self.manifest, self.payload)
        self.server.writer = None
        self.assertEqual(self.reader.get(self.manifest), self.payload)
        self.assertEqual(self.request(self.path(), token=_WRITER)[0], 401)
        self.assertEqual(
            self.request(self.path(object=True), self.payload, token=_READER)[0], 403
        )
        path = (
            self.server.cache.root
            / "package"
            / "objects"
            / self.manifest.payload_identity.digest
        )
        alias = self.root / "hardlink"
        os.link(path, alias)
        self.assertEqual(self.request(self.path(object=True), token=_READER)[0], 409)

    def test_namespace_policy_missing_payload_and_oversized_committed_entry(self):
        readonly = replace(
            self.configuration,
            policies=tuple(
                replace(policy, mode=SharedCacheAccessMode.READ_ONLY)
                for policy in self.configuration.policies
            ),
        )
        self.server.configuration = readonly
        self.assertEqual(self.request(self.path(object=True), self.payload)[0], 403)
        self.server.configuration = self.configuration
        self.server.cache.configuration = replace(_configuration(), maximum_bytes=1)
        self.assertEqual(self.request(self.path(object=True), self.payload)[0], 201)
        self.assertEqual(
            self.request(self.path(), canonical_json_bytes(self.manifest.to_dict()))[0],
            409,
        )
        self.assertFalse((self.server.cache.root / "package" / "manifests").exists())


if __name__ == "__main__":
    unittest.main()
