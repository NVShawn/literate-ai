"""Explicitly started loopback reference service for the ADR 0044 wire protocol.

One instance owns one configured writer group. This is opaque byte custody,
never artifact admission. TLS and remote exposure belong to an operator proxy.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from literate_ai.adapters.cache.shared_artifacts import (
    LocalSharedArtifactCache,
    SharedArtifactCacheError,
    SharedCacheArtifactManifest,
    _bytes_identity,
)
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.shared_cache import (
    SharedCacheConfiguration,
    SharedCacheNamespace,
)

_MANIFEST_BYTES = 1024 * 1024
_DIGEST = r"[0-9a-f]{64}"


class ReferenceSharedCacheServer(ThreadingHTTPServer):
    """Finite in-memory publication staging plus the existing local cache owner.

    Credentials are supplied privately by the caller and never logged. Bind only
    loopback; a separately operated TLS proxy is required for remote clients.
    ``maximum_bytes`` remains the local owner's quota *per artifact namespace*.
    Pending bytes are a separate aggregate budget across those namespaces.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        configuration: SharedCacheConfiguration,
        root: Path,
        *,
        reader_token: str,
        writer_token: str | None = None,
        port: int = 0,
        pending_bytes: int = 64 * 1024 * 1024,
        maximum_object_bytes: int = 64 * 1024 * 1024,
        pending_seconds: int = 60,
        maximum_connections: int = 4,
        deadline_seconds: int = 30,
    ) -> None:
        for name, value, maximum in (
            ("pending_bytes", pending_bytes, 4 * 1024**3),
            ("maximum_object_bytes", maximum_object_bytes, 1024**3),
            ("pending_seconds", pending_seconds, 3600),
            ("maximum_connections", maximum_connections, 32),
            ("deadline_seconds", deadline_seconds, 300),
        ):
            if type(value) is not int or not 0 < value <= maximum:
                raise ValueError(f"{name} must be a positive bounded integer")
        if maximum_object_bytes > pending_bytes:
            raise ValueError("one object must fit the pending byte budget")
        for token in (reader_token,) + (
            () if writer_token is None else (writer_token,)
        ):
            if (
                not isinstance(token, str)
                or not 16 <= len(token) <= 8192
                or any(not 33 <= ord(char) <= 126 for char in token)
            ):
                raise ValueError("credentials must be private printable ASCII tokens")
        if reader_token == writer_token:
            raise ValueError("reader and writer credentials must be distinct")
        self.cache = LocalSharedArtifactCache(configuration, root)
        self.configuration = configuration
        prefix = (
            ""
            if configuration.endpoint is None
            else urlsplit(configuration.endpoint).path.rstrip("/")
        )
        # Never accept escaped, ambiguous or traversal-bearing operator prefixes.
        if prefix and re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+", prefix) is None:
            raise ValueError("endpoint path must contain plain portable segments")
        self.route = re.compile(
            re.escape(prefix + "/" + configuration.namespace)
            + rf"/([a-z]+)/(?:(objects)/({_DIGEST})|(manifests)/({_DIGEST})\.json)"
        )
        self.reader = "Bearer " + reader_token
        self.writer = None if writer_token is None else "Bearer " + writer_token
        self.pending_limit = pending_bytes
        self.object_limit = min(maximum_object_bytes, configuration.maximum_bytes)
        self.pending_seconds = pending_seconds
        self.deadline = deadline_seconds
        self.pending: dict[tuple[SharedCacheNamespace, str], tuple[bytes, float]] = {}
        self.publication_lock = threading.Lock()
        self.connections = threading.BoundedSemaphore(maximum_connections)
        super().__init__(("127.0.0.1", port), _Handler)

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        # Covers slow headers and trickle bodies as well as ordinary idle reads.
        def expire():
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(self.deadline, expire)
        timer.daemon = True
        request.settimeout(self.deadline)
        timer.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            timer.cancel()
            self.connections.release()

    def handle_error(self, request, client_address):
        # No URL, request body, credential, raw exception or private path logging.
        pass

    def _expire_pending(self) -> None:
        cutoff = time.monotonic() - self.pending_seconds
        for key in tuple(self.pending):
            if self.pending[key][1] < cutoff:
                del self.pending[key]

    def _read(self, namespace, kind, digest) -> bytes | None:
        if kind == "objects":
            staged = self.pending.get((namespace, digest))
            if staged is not None:
                return staged[0]
        path = (
            self.cache.root
            / namespace.value
            / kind
            / (digest + (".json" if kind == "manifests" else ""))
        )
        if not path.exists():
            return None
        if path.lstat().st_nlink != 1:
            raise SharedArtifactCacheError("shared_cache.path_unsafe", "linked entry")
        content = self.cache._read_regular(
            path, _MANIFEST_BYTES if kind == "manifests" else self.object_limit
        )
        if kind == "objects":
            if _bytes_identity(content).digest != digest:
                raise SharedArtifactCacheError(
                    "shared_cache.payload_corrupt", "corrupt object"
                )
        else:
            manifest = SharedCacheArtifactManifest.from_dict(json.loads(content))
            if (
                manifest.namespace != namespace
                or manifest.key_identity.digest != digest
                or manifest.size_bytes > self.object_limit
            ):
                raise SharedArtifactCacheError(
                    "shared_cache.manifest_corrupt", "misplaced manifest"
                )
            if self.cache.get(manifest) is None:
                return None
        return content

    def _put(self, namespace, kind, digest, content) -> int:
        if kind == "objects":
            if _bytes_identity(content).digest != digest:
                return 422
            if self._read(namespace, kind, digest) is not None:
                return 412
            if (
                len(self.pending) >= 1024
                or sum(len(entry[0]) for entry in self.pending.values()) + len(content)
                > self.pending_limit
            ):
                return 507
            self.pending[(namespace, digest)] = (content, time.monotonic())
            return 201
        manifest = SharedCacheArtifactManifest.from_dict(json.loads(content))
        if (
            manifest.namespace != namespace
            or manifest.key_identity.digest != digest
            or canonical_json_bytes(manifest.to_dict()) != content
            or manifest.size_bytes > self.object_limit
        ):
            return 422
        payload = self._read(namespace, "objects", manifest.payload_identity.digest)
        if payload is None:
            return 409  # Payload-before-manifest is mandatory.
        existing = self._read(namespace, kind, digest)
        # The local owner checks exact collisions before eviction, then applies
        # its own inventory, retention, quota, locks and atomic publication.
        self.cache.put(manifest, payload)
        self.pending.pop((namespace, manifest.payload_identity.digest), None)
        return 201 if existing is None else 412


class _Handler(BaseHTTPRequestHandler):
    server_version = "LiterateAIReferenceCache"
    sys_version = ""

    def log_message(self, *_args):
        pass

    def do_GET(self):
        self._dispatch(False)

    def do_PUT(self):
        self._dispatch(True)

    def _reply(self, status: int, content: bytes = b""):
        self.send_response(status)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if content:
            self.wfile.write(content)

    def _dispatch(self, writing: bool):
        server = self.server
        supplied = self.headers.get_all("Authorization", [])
        authorized_writer = (
            len(supplied) == 1
            and server.writer is not None
            and hmac.compare_digest(supplied[0].encode(), server.writer.encode())
        )
        authorized_reader = len(supplied) == 1 and hmac.compare_digest(
            supplied[0].encode(), server.reader.encode()
        )
        if not authorized_writer and not authorized_reader:
            self._reply(401)
            return
        if writing and not authorized_writer:
            self._reply(403)
            return
        match = server.route.fullmatch(self.path)
        if match is None:
            self._reply(404)
            return
        try:
            namespace = SharedCacheNamespace(match[1])
            policy = server.configuration.policy(namespace)
            kind, digest = (match[2], match[3]) if match[2] else (match[4], match[5])
            if writing:
                if not policy.mode.can_write:
                    self._reply(403)
                    return
                if self.headers.get_all("If-None-Match", []) != ["*"]:
                    self._reply(428)
                    return
                if self.headers.get("Expect") is not None:
                    self._reply(417)
                    return
                if self.headers.get("Content-Encoding", "identity") != "identity":
                    self._reply(415)
                    return
                lengths = self.headers.get_all("Content-Length", [])
                if (
                    self.headers.get_all("Transfer-Encoding", [])
                    or len(lengths) != 1
                    or re.fullmatch(r"[0-9]{1,12}", lengths[0]) is None
                ):
                    self._reply(400)
                    return
                length = int(lengths[0])
                if length > (
                    _MANIFEST_BYTES if kind == "manifests" else server.object_limit
                ):
                    self._reply(413)
                    return
                content = self.rfile.read(length)
                if len(content) != length:
                    self._reply(400)
                    return
            with server.publication_lock:
                server._expire_pending()
                if writing:
                    status = server._put(namespace, kind, digest, content)
                    content = b""
                else:
                    content = server._read(namespace, kind, digest)
                    status = 404 if content is None else 200
            self._reply(status, b"" if content is None else content)
        except SharedArtifactCacheError as exc:
            self._reply(507 if exc.code == "shared_cache.quota_exhausted" else 409)
        except (ValueError, TypeError):
            self._reply(422)
        except OSError:
            # A disconnected writer never creates a partial staged object.
            self.close_connection = True


__all__ = ["ReferenceSharedCacheServer"]


def main() -> int:
    """Start only on explicit operator invocation, with private configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--pending-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--maximum-object-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--maximum-connections", type=int, default=4)
    parser.add_argument("--pending-seconds", type=int, default=60)
    parser.add_argument("--deadline-seconds", type=int, default=30)
    args = parser.parse_args()
    try:
        encoded = LocalSharedArtifactCache._read_regular(
            args.configuration.absolute(), _MANIFEST_BYTES
        )
        configuration = SharedCacheConfiguration.from_dict(json.loads(encoded))
        with ReferenceSharedCacheServer(
            configuration,
            args.root,
            reader_token=os.environ.get("LITAI_CACHE_READER_TOKEN"),
            writer_token=os.environ.get("LITAI_CACHE_WRITER_TOKEN"),
            port=args.port,
            pending_bytes=args.pending_bytes,
            maximum_object_bytes=args.maximum_object_bytes,
            maximum_connections=args.maximum_connections,
            pending_seconds=args.pending_seconds,
            deadline_seconds=args.deadline_seconds,
        ) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (ValueError, TypeError, OSError, SharedArtifactCacheError):
        # Do not echo environment values, configuration or filesystem paths.
        parser.exit(
            2,
            "Reference cache refused startup; check private configuration "
            "and budgets.\n",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
