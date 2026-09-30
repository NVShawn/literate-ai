"""Independent service probes reach the selected local process, not a proxy."""

import json
import threading
import unittest
import urllib.error
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import Mock, patch

from literate_ai.adapters.component_acceptance import ServiceHttpProbe
from literate_ai.adapters.lifecycle._service_http import open_service_request
from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecyclePorts


@contextmanager
def serve(routes):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            status, headers, body = routes.get(self.path, (503, {}, b"wrong server"))
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def probe(path="/value"):
    return ServiceHttpProbe(
        "GET", path, (), None, 200, (), {"value": 7}, (), (), 1, 1024, True
    )


class ServiceHttpTransportTests(unittest.TestCase):
    def test_global_opener_cannot_intercept_local_acceptance(self):
        global_opener = Mock()
        global_opener.open.side_effect = AssertionError("ambient opener was used")
        with (
            serve({"/value": (200, {}, b'{"value":7}')}) as (base_url, requests),
            patch("urllib.request._opener", global_opener),
        ):
            observations = []
            LocalStandardLifecyclePorts._observe_service_request(
                probe(), base_url, observations
            )
            self.assertEqual(len(observations), 1)
            self.assertEqual(requests, ["/value"])
            global_opener.open.assert_not_called()

    def test_non_loopback_or_credentialed_origin_is_refused_before_connect(self):
        for url in (
            "http://example.invalid/value",
            "https://127.0.0.1/value",
            "http://user:password@127.0.0.1/value",
            "http://127.0.0.1:0/value",
            "http://127.0.0.1:invalid/value",
        ):
            with (
                self.subTest(url=url),
                patch("urllib.request.HTTPHandler.http_open") as connect,
                self.assertRaises(urllib.error.URLError),
            ):
                open_service_request(url, timeout=1)
            connect.assert_not_called()

    def test_all_local_probe_paths_bypass_ambient_proxy(self):
        body = b'{"value":7}'
        with (
            serve({"/value": (200, {}, body)}) as (base_url, requests),
            serve({}) as (proxy_url, proxy_requests),
            patch("urllib.request.getproxies", return_value={"http": proxy_url}),
            patch("urllib.request.proxy_bypass", return_value=False),
            patch("urllib.request._opener", None),
        ):
            for kind in ("service", "frontend", "ipc"):
                with self.subTest(kind=kind):
                    if kind == "service":
                        observations = []
                        LocalStandardLifecyclePorts._observe_service_request(
                            probe(), base_url, observations
                        )
                        self.assertEqual(len(observations), 1)
                    elif kind == "frontend":
                        self.assertTrue(
                            LocalStandardLifecyclePorts._frontend_is_ready(
                                base_url + "/value"
                            )
                        )
                    else:
                        self.assertEqual(
                            LocalStandardLifecyclePorts._http_get_body(
                                base_url, probe()
                            ),
                            body,
                        )
            self.assertEqual(proxy_requests, [])
            self.assertEqual(requests, ["/value"] * 3)

    def test_redirect_cannot_substitute_another_local_server(self):
        with serve({"/value": (200, {}, b'{"value":7}')}) as (other, other_requests):
            with serve({"/value": (302, {"Location": other + "/value"}, b"")}) as (
                base_url,
                requests,
            ):
                with self.assertRaises(urllib.error.URLError):
                    LocalStandardLifecyclePorts._observe_service_request(
                        probe(), base_url, []
                    )
                self.assertFalse(
                    LocalStandardLifecyclePorts._frontend_is_ready(base_url + "/value")
                )
                self.assertIsNone(
                    LocalStandardLifecyclePorts._http_get_body(base_url, probe())
                )
                self.assertEqual(requests, ["/value"] * 3)
                self.assertEqual(other_requests, [])

    def test_same_origin_redirect_preserves_the_declared_response(self):
        body = json.dumps({"value": 7}).encode()
        with serve(
            {
                "/start": (302, {"Location": "/value"}, b""),
                "/value": (200, {}, body),
            }
        ) as (base_url, requests):
            observations = []
            LocalStandardLifecyclePorts._observe_service_request(
                probe("/start"), base_url, observations
            )
            self.assertEqual(len(observations), 1)
            self.assertEqual(requests, ["/start", "/value"])
