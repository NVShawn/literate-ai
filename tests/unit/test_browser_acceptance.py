"""Fail-closed browser interaction acceptance (ADR 0028 / issue #218).

When Playwright + Chromium are installed, hand-authored HTML served over HTTP is
driven through the real ``PlaywrightPageDriver``; a missing engine must fail
closed rather than skip.
"""

from __future__ import annotations

import importlib.util
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from literate_ai.adapters.browser_acceptance import (
    BrowserAcceptanceError,
    PlaywrightPageDriver,
    decide_browser_acceptance,
)
from literate_ai.adapters.component_acceptance import (
    BrowserInteractionAcceptance,
    BrowserPostcondition,
    BrowserViewport,
)

_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None


def _chromium_available() -> bool:
    if not _PLAYWRIGHT:
        return False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            browser.close()
        return True
    except Exception:
        return False


_CHROMIUM = _chromium_available()


DESKTOP = BrowserViewport("desktop", 1280, 800)


def _contract(
    *,
    viewports=(DESKTOP,),
    steps=(),
    postconditions=(),
    required_roles=(),
    required_landmarks=(),
    rejection_classes=frozenset(
        {"console-error", "page-error", "failed-request", "http-error"}
    ),
    reject_horizontal_overflow=True,
    expected_request_count=None,
) -> BrowserInteractionAcceptance:
    return BrowserInteractionAcceptance(
        component="frontend",
        specification_set_identity="sha256:spec",
        arguments=(),
        environment=(),
        readiness_path="/health",
        viewports=tuple(viewports),
        steps=tuple(steps),
        postconditions=tuple(postconditions),
        required_roles=tuple(required_roles),
        required_landmarks=tuple(required_landmarks),
        rejection_classes=rejection_classes,
        reject_horizontal_overflow=reject_horizontal_overflow,
        expected_request_count=expected_request_count,
        startup_timeout_seconds=5.0,
        process_timeout_seconds=30.0,
        shutdown_timeout_seconds=5.0,
        stdout_limit_bytes=1024 * 1024,
        stderr_limit_bytes=1024 * 1024,
    )


# --------------------------------------------------------------------------- #
# Hand-authored HTML fixtures, driven through the real PlaywrightPageDriver.
# Each maps to an issue #218 "Acceptance" bullet. Skipped when Chromium is
# unavailable; the fake-port tests above already prove the decision logic.
# --------------------------------------------------------------------------- #


NOOP_EXPORT_HTML = """
<!doctype html><html><head><title>Data</title></head>
<body><main><button id="export">Export</button></main>
<script>document.getElementById('export').addEventListener('click', () => {});</script>
</body></html>
"""


WORKING_EXPORT_HTML = """
<!doctype html><html><head><title>Data</title></head>
<body><main><a id="export" download="data.csv"
href="data:text/csv;charset=utf-8,a,b%0A1,2">Export</a></main></body></html>
"""


def _serve(html: str) -> tuple[ThreadingHTTPServer, str, threading.Thread]:
    body = html.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
                return
            if self.path.startswith("/api/"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"[]")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", thread


@unittest.skipUnless(_CHROMIUM, "Playwright Chromium is not installed")
class PlaywrightRealBrowserTests(unittest.TestCase):
    """Real Chromium confirms the fixtures produce the scripted observations."""

    def _drive(self, html: str, contract: BrowserInteractionAcceptance):
        server, base_url, thread = _serve(html)
        driver = PlaywrightPageDriver()
        try:
            viewport = contract.viewports[0]
            observation = driver.observe(base_url, viewport, contract)
            return decide_browser_acceptance(contract, viewport, observation)
        finally:
            server.shutdown()
            thread.join(timeout=2)

    def test_noop_export_fails_closed(self) -> None:
        contract = _contract(
            steps=(),
            postconditions=(
                BrowserPostcondition("csv-download", "download", "#export"),
            ),
        )
        with self.assertRaises(BrowserAcceptanceError) as caught:
            self._drive(NOOP_EXPORT_HTML, contract)
        self.assertEqual(
            caught.exception.code, "browser_acceptance.postcondition_failed"
        )

    def test_working_export_and_landmark_is_accepted(self) -> None:
        contract = _contract(
            postconditions=(
                BrowserPostcondition("csv-download", "download", "#export"),
            ),
            required_landmarks=("main",),
        )
        evidence = self._drive(WORKING_EXPORT_HTML, contract)
        self.assertEqual(evidence["outcome"], "accepted")


class PlaywrightEngineUnavailableTests(unittest.TestCase):
    def test_missing_engine_fails_closed_not_skipped(self) -> None:
        # Simulate Playwright not installed: the lazy import inside observe()
        # must raise a typed BrowserAcceptanceError, never silently pass.
        driver = PlaywrightPageDriver()
        with mock.patch.dict(
            "sys.modules", {"playwright": None, "playwright.sync_api": None}
        ):
            with self.assertRaises(BrowserAcceptanceError) as caught:
                driver.observe("http://127.0.0.1:1", DESKTOP, _contract())
        self.assertEqual(caught.exception.code, "browser_acceptance.engine_unavailable")


if __name__ == "__main__":
    unittest.main()
