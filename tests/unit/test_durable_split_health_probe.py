"""Health readiness must accept a successful empty GET /health."""

from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar
from unittest import mock

from tests.conformance.support.durable_split_service import (
    DurableSplitPortfolioError,
    _http_bytes,
    _http_health,
)


class _StatusHandler(BaseHTTPRequestHandler):
    status: ClassVar[int] = 200
    body: ClassVar[bytes] = b""

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self.send_response(self.status)
        self.end_headers()
        if self.body:
            self.wfile.write(self.body)


class DurableSplitHealthProbeTests(unittest.TestCase):
    def _serve(self, status: int, body: bytes = b"") -> str:
        _StatusHandler.status = status
        _StatusHandler.body = body
        server = HTTPServer(("127.0.0.1", 0), _StatusHandler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(
            target=server.serve_forever, name="health-probe-fixture", daemon=True
        )

        def stop_server() -> None:
            # shutdown() deadlocks if serve_forever() was never started.
            if thread.ident is None:
                return
            server.shutdown()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "health-probe fixture did not stop")

        # unittest runs these in reverse order: stop/join, then close the socket.
        self.addCleanup(stop_server)
        thread.start()
        host, port = server.server_address[:2]
        return f"http://{host}:{port}/health"

    def test_health_probe_accepts_http_200(self) -> None:
        _http_health(self._serve(200, b'{"status":"ok"}'))

    def test_health_probe_accepts_http_204(self) -> None:
        _http_health(self._serve(204))

    def test_health_probe_rejects_http_503(self) -> None:
        url = self._serve(503, b"unavailable")
        with self.assertRaises(DurableSplitPortfolioError) as raised:
            _http_health(url)
        self.assertIn("HTTP 503", str(raised.exception))

    def test_snapshot_and_page_fetches_still_require_http_200(self) -> None:
        url = self._serve(204)
        with self.assertRaises(DurableSplitPortfolioError) as raised:
            _http_bytes(url)
        self.assertIn("HTTP 204", str(raised.exception))


class HealthProbeFixtureCleanupTests(unittest.TestCase):
    def test_cleanup_stops_and_joins_before_closing(self) -> None:
        fixture = DurableSplitHealthProbeTests()
        calls = mock.Mock()
        with (
            mock.patch(__name__ + ".HTTPServer", autospec=True) as server_type,
            mock.patch(__name__ + ".threading.Thread", autospec=True) as thread_type,
        ):
            server = server_type.return_value
            server.server_address = ("127.0.0.1", 12345)
            thread = thread_type.return_value
            thread.ident = 1
            thread.is_alive.return_value = False
            calls.attach_mock(thread.start, "start")
            calls.attach_mock(server.shutdown, "shutdown")
            calls.attach_mock(thread.join, "join")
            calls.attach_mock(thread.is_alive, "is_alive")
            calls.attach_mock(server.server_close, "close")
            self.assertEqual(fixture._serve(204), "http://127.0.0.1:12345/health")
            self.assertTrue(fixture.doCleanups())
        self.assertEqual(
            calls.mock_calls,
            [
                mock.call.start(),
                mock.call.shutdown(),
                mock.call.join(timeout=5),
                mock.call.is_alive(),
                mock.call.close(),
            ],
        )

    def test_failed_thread_start_closes_without_shutdown(self) -> None:
        fixture = DurableSplitHealthProbeTests()
        with (
            mock.patch(__name__ + ".HTTPServer", autospec=True) as server_type,
            mock.patch(__name__ + ".threading.Thread", autospec=True) as thread_type,
        ):
            thread = thread_type.return_value
            thread.ident = None
            thread.start.side_effect = RuntimeError("cannot start fixture thread")
            with self.assertRaisesRegex(RuntimeError, "cannot start fixture thread"):
                fixture._serve(204)
            self.assertTrue(fixture.doCleanups())
            server_type.return_value.shutdown.assert_not_called()
            thread.join.assert_not_called()
            server_type.return_value.server_close.assert_called_once_with()

    def test_failed_test_body_leaves_no_live_thread_or_open_socket(self) -> None:
        owned_servers: list[HTTPServer] = []
        owned_threads: list[threading.Thread] = []
        server_factory = HTTPServer
        thread_factory = threading.Thread

        def server(*args: object, **kwargs: object) -> HTTPServer:
            instance = server_factory(*args, **kwargs)
            owned_servers.append(instance)
            return instance

        def thread(*args: object, **kwargs: object) -> threading.Thread:
            instance = thread_factory(*args, **kwargs)
            owned_threads.append(instance)
            return instance

        class FailingFixture(DurableSplitHealthProbeTests):
            def runTest(self) -> None:
                _http_health(self._serve(204))
                raise RuntimeError("intentional fixture body failure")

        result = unittest.TestResult()
        with (
            mock.patch(__name__ + ".HTTPServer", side_effect=server),
            mock.patch(__name__ + ".threading.Thread", side_effect=thread),
        ):
            FailingFixture().run(result)
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("intentional fixture body failure", result.errors[0][1])
        self.assertFalse(result.failures)
        self.assertEqual(len(owned_threads), 1)
        self.assertFalse(owned_threads[0].is_alive())
        self.assertEqual(len(owned_servers), 1)
        self.assertEqual(owned_servers[0].fileno(), -1)


if __name__ == "__main__":
    unittest.main()
