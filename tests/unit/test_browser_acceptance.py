"""Fail-closed browser interaction acceptance (ADR 0028 / issue #218).

The decision logic (given a browser observation, does it pass or fail?) is proven
here with a scripted fake driver and no real browser, so the contract's
fail-closed behaviour is testable everywhere. When Playwright + Chromium are
installed, the same fixtures are additionally driven through the real
``PlaywrightPageDriver`` against hand-authored HTML served over HTTP, proving the
engine surfaces the same observations the fake scripts.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.browser_acceptance import (
    BrowserAcceptanceError,
    BrowserConsoleFailure,
    BrowserObservation,
    BrowserPageDriver,
    PlaywrightPageDriver,
    decide_browser_acceptance,
)
from literate_ai.adapters.component_acceptance import (
    BROWSER_SCHEMA,
    BrowserInteractionAcceptance,
    BrowserInteractionStep,
    BrowserPostcondition,
    BrowserViewport,
    ComponentAcceptanceError,
    load_browser_interaction_acceptance,
    oracle_path,
    resolve_component_acceptance_oracle,
)
from literate_ai.contracts import ContentIdentity, canonical_identity

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
MOBILE = BrowserViewport("mobile", 390, 844)


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


class ScriptedDriver(BrowserPageDriver):
    """A fake browser-port implementation returning a scripted observation.

    Proves the contract's decision logic without a real browser: it returns
    whatever console errors / overflow / request counts a test scripts.
    """

    def __init__(self, observation: BrowserObservation) -> None:
        self._observation = observation

    @property
    def tool_identity(self) -> ContentIdentity:
        return canonical_identity(
            {"schema": "literate-ai/browser-driver@1", "engine": "scripted-fake"}
        )

    def observe(self, base_url, viewport, contract) -> BrowserObservation:
        return self._observation


class BrowserAcceptanceDecisionTests(unittest.TestCase):
    """The pure fail-closed decision -- no browser engine involved."""

    def test_clean_observation_is_accepted(self) -> None:
        observation = BrowserObservation(
            viewport_width=1280,
            viewport_height=800,
            document_scroll_width=1280,
        )
        evidence = decide_browser_acceptance(_contract(), DESKTOP, observation)
        self.assertEqual(evidence["outcome"], "accepted")

    def test_throwing_init_surfaces_a_console_error_and_fails(self) -> None:
        # Fixture: page contains the expected HTML text but throws on init. Text
        # acceptance passes; a browser surfaces the console/page error.
        observation = BrowserObservation(
            viewport_width=1280,
            viewport_height=800,
            document_scroll_width=1280,
            console_failures=(
                BrowserConsoleFailure(
                    "page-error", "ReferenceError: TOTAL is not defined"
                ),
            ),
        )
        with self.assertRaises(BrowserAcceptanceError) as caught:
            decide_browser_acceptance(_contract(), DESKTOP, observation)
        self.assertEqual(caught.exception.code, "browser_acceptance.console_failure")

    def test_unbound_scroll_handler_fails_its_postcondition(self) -> None:
        # Fixture: virtual grid whose scroll handler is never bound; after the
        # scroll step the expected row never appears.
        contract = _contract(
            steps=(BrowserInteractionStep("scroll", "#grid", "5000"),),
            postconditions=(
                BrowserPostcondition("row-9999", "row-identity", ".row", "Row 9999"),
            ),
        )
        observation = BrowserObservation(
            viewport_width=1280,
            viewport_height=800,
            document_scroll_width=1280,
            resolved_postconditions=(("row-9999", False),),
        )
        with self.assertRaises(BrowserAcceptanceError) as caught:
            decide_browser_acceptance(contract, DESKTOP, observation)
        self.assertEqual(
            caught.exception.code, "browser_acceptance.postcondition_failed"
        )

    def test_noop_export_button_fails_a_download_assertion(self) -> None:
        contract = _contract(
            steps=(BrowserInteractionStep("click", "#export"),),
            postconditions=(
                BrowserPostcondition("csv-download", "download", "#export"),
            ),
        )
        observation = BrowserObservation(
            viewport_width=1280,
            viewport_height=800,
            document_scroll_width=1280,
            resolved_postconditions=(("csv-download", False),),
        )
        with self.assertRaises(BrowserAcceptanceError) as caught:
            decide_browser_acceptance(contract, DESKTOP, observation)
        self.assertEqual(
            caught.exception.code, "browser_acceptance.postcondition_failed"
        )

    def test_page_wider_than_mobile_viewport_fails_overflow(self) -> None:
        observation = BrowserObservation(
            viewport_width=390,
            viewport_height=844,
            document_scroll_width=520,
        )
        with self.assertRaises(BrowserAcceptanceError) as caught:
            decide_browser_acceptance(
                _contract(viewports=(MOBILE,)), MOBILE, observation
            )
        self.assertEqual(caught.exception.code, "browser_acceptance.document_overflow")

    def test_local_toggle_that_fetches_fails_request_count(self) -> None:
        contract = _contract(
            steps=(BrowserInteractionStep("click", "#sort"),),
            expected_request_count=0,
        )
        observation = BrowserObservation(
            viewport_width=1280,
            viewport_height=800,
            document_scroll_width=1280,
            request_count=1,
        )
        with self.assertRaises(BrowserAcceptanceError) as caught:
            decide_browser_acceptance(contract, DESKTOP, observation)
        self.assertEqual(caught.exception.code, "browser_acceptance.request_count")

    def test_local_toggle_with_no_fetch_is_accepted(self) -> None:
        contract = _contract(expected_request_count=0)
        observation = BrowserObservation(
            viewport_width=1280,
            viewport_height=800,
            document_scroll_width=1280,
            request_count=0,
        )
        evidence = decide_browser_acceptance(contract, DESKTOP, observation)
        self.assertEqual(evidence["request_count"], 0)

    def test_missing_required_role_fails(self) -> None:
        contract = _contract(required_roles=(("button", "Export"),))
        observation = BrowserObservation(
            viewport_width=1280, viewport_height=800, document_scroll_width=1280
        )
        with self.assertRaises(BrowserAcceptanceError) as caught:
            decide_browser_acceptance(contract, DESKTOP, observation)
        self.assertEqual(caught.exception.code, "browser_acceptance.role_missing")

    def test_missing_required_landmark_fails(self) -> None:
        contract = _contract(required_landmarks=("main",))
        observation = BrowserObservation(
            viewport_width=1280, viewport_height=800, document_scroll_width=1280
        )
        with self.assertRaises(BrowserAcceptanceError) as caught:
            decide_browser_acceptance(contract, DESKTOP, observation)
        self.assertEqual(caught.exception.code, "browser_acceptance.landmark_missing")

    def test_http_error_response_is_rejected(self) -> None:
        observation = BrowserObservation(
            viewport_width=1280,
            viewport_height=800,
            document_scroll_width=1280,
            console_failures=(BrowserConsoleFailure("http-error", "500 /api/data"),),
        )
        with self.assertRaises(BrowserAcceptanceError) as caught:
            decide_browser_acceptance(_contract(), DESKTOP, observation)
        self.assertEqual(caught.exception.code, "browser_acceptance.console_failure")

    def test_disabled_rejection_class_does_not_fail(self) -> None:
        # A contract that does not enable failed-request rejection ignores it.
        contract = _contract(rejection_classes=frozenset({"console-error"}))
        observation = BrowserObservation(
            viewport_width=1280,
            viewport_height=800,
            document_scroll_width=1280,
            console_failures=(
                BrowserConsoleFailure("failed-request", "GET /favicon.ico"),
            ),
        )
        evidence = decide_browser_acceptance(contract, DESKTOP, observation)
        self.assertEqual(evidence["outcome"], "accepted")

    def test_evidence_receipt_binds_the_full_interaction_context(self) -> None:
        observation = BrowserObservation(
            viewport_width=390,
            viewport_height=844,
            document_scroll_width=390,
            console_failures=(BrowserConsoleFailure("console-warning", "noise"),),
            request_count=0,
            resolved_postconditions=(("row-1", True),),
            observed_landmarks=("main",),
            screenshot=b"png-bytes",
            accessibility_snapshot=b"{}",
        )
        contract = _contract(
            viewports=(MOBILE,),
            postconditions=(
                BrowserPostcondition("row-1", "row-identity", ".row", "Row 1"),
            ),
            required_landmarks=("main",),
            rejection_classes=frozenset({"console-error"}),
        )
        evidence = decide_browser_acceptance(contract, MOBILE, observation)
        self.assertEqual(
            evidence["viewport"], {"label": "mobile", "width": 390, "height": 844}
        )
        self.assertEqual(evidence["request_count"], 0)
        self.assertEqual(evidence["observed_landmarks"], ["main"])
        self.assertEqual(
            evidence["resolved_postconditions"],
            [{"postcondition_id": "row-1", "satisfied": True}],
        )
        self.assertTrue(evidence["screenshot_identity"].startswith("sha256:"))
        self.assertTrue(
            evidence["accessibility_snapshot_identity"].startswith("sha256:")
        )
        # Console failures are recorded as diagnostics even when not a rejection
        # class, so a reviewer sees exactly what the browser surfaced.
        self.assertEqual(len(evidence["console_failures"]), 1)


class BrowserContractLoaderTests(unittest.TestCase):
    def _write(self, body: dict) -> Path:
        directory = Path(tempfile.mkdtemp())
        path = directory / "frontend.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        return path

    def _valid_document(self) -> dict:
        return {
            "schema": BROWSER_SCHEMA,
            "specification_set_identity": "sha256:spec",
            "process": {"arguments": ["{port}"]},
            "readiness_path": "/health",
            "viewports": [
                {"label": "desktop", "width": 1280, "height": 800},
                {"label": "mobile", "width": 390, "height": 844},
            ],
            "steps": [{"action": "click", "selector": "#sort"}],
            "postconditions": [
                {
                    "postcondition_id": "row-1",
                    "kind": "row-identity",
                    "selector": ".row",
                    "expected": "Row 1",
                }
            ],
            "required_roles": [{"role": "button", "name": "Export"}],
            "required_landmarks": ["main"],
            "rejection_classes": ["console-error", "page-error"],
            "reject_horizontal_overflow": True,
            "expected_request_count": 0,
        }

    def test_loads_a_valid_contract(self) -> None:
        contract = load_browser_interaction_acceptance(
            self._write(self._valid_document()), "frontend"
        )
        self.assertEqual(contract.component_name, "frontend")
        self.assertEqual(len(contract.viewports), 2)
        self.assertEqual(contract.expected_request_count, 0)
        self.assertIn("console-error", contract.rejection_classes)
        self.assertIsInstance(contract.identity, ContentIdentity)

    def test_rejects_unknown_top_level_fields(self) -> None:
        document = self._valid_document()
        document["surprise"] = True
        with self.assertRaises(ComponentAcceptanceError) as caught:
            load_browser_interaction_acceptance(self._write(document), "frontend")
        self.assertEqual(caught.exception.code, "component_acceptance.contract_invalid")

    def test_rejects_unbound_specification_identity(self) -> None:
        document = self._valid_document()
        document["specification_set_identity"] = ""
        with self.assertRaises(ComponentAcceptanceError) as caught:
            load_browser_interaction_acceptance(self._write(document), "frontend")
        self.assertEqual(caught.exception.code, "component_acceptance.oracle_unbound")

    def test_rejects_invalid_step_action(self) -> None:
        document = self._valid_document()
        document["steps"] = [{"action": "teleport", "selector": "#x"}]
        with self.assertRaises(ComponentAcceptanceError):
            load_browser_interaction_acceptance(self._write(document), "frontend")

    def test_rejects_empty_viewports(self) -> None:
        document = self._valid_document()
        document["viewports"] = []
        with self.assertRaises(ComponentAcceptanceError) as caught:
            load_browser_interaction_acceptance(self._write(document), "frontend")
        self.assertEqual(caught.exception.code, "component_acceptance.oracle_empty")

    def test_rejects_bad_rejection_class(self) -> None:
        document = self._valid_document()
        document["rejection_classes"] = ["not-a-class"]
        with self.assertRaises(ComponentAcceptanceError):
            load_browser_interaction_acceptance(self._write(document), "frontend")

    def test_require_current_fails_on_specification_drift(self) -> None:
        contract = load_browser_interaction_acceptance(
            self._write(self._valid_document()), "frontend"
        )
        lock = mock.Mock()
        lock.root_revision = "root"
        lock.nodes = (
            SimpleNamespace(
                revision=SimpleNamespace(
                    identity="root",
                    specification_set_identity=SimpleNamespace(uri="sha256:new"),
                )
            ),
        )
        with self.assertRaises(ComponentAcceptanceError) as caught:
            contract.require_current(lock)
        self.assertEqual(
            caught.exception.code, "component_acceptance.interface_changed"
        )

    def test_oracle_dispatch_routes_web_application_to_browser_contract(self) -> None:
        # A web-application entrypoint resolves to the browser-interaction
        # contract, leaving persistent-service and portable-application untouched.
        root = Path(tempfile.mkdtemp())
        path = oracle_path(root, "frontend")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self._valid_document()), encoding="utf-8")
        revision = canonical_identity({"root": "frontend"})
        lock = SimpleNamespace(
            root_revision=revision,
            nodes=(
                SimpleNamespace(
                    revision=SimpleNamespace(
                        identity=revision,
                        specification_set_identity=SimpleNamespace(uri="sha256:spec"),
                        definition=SimpleNamespace(
                            coordinate=SimpleNamespace(name="frontend"),
                            entrypoints=(SimpleNamespace(kind="web-application"),),
                        ),
                    )
                ),
            ),
        )
        oracle = resolve_component_acceptance_oracle(root, "components/frontend", lock)
        self.assertIsInstance(oracle, BrowserInteractionAcceptance)
        self.assertEqual(oracle.component_name, "frontend")


# --------------------------------------------------------------------------- #
# Hand-authored HTML fixtures, driven through the real PlaywrightPageDriver.
# Each maps to an issue #218 "Acceptance" bullet. Skipped when Chromium is
# unavailable; the fake-port tests above already prove the decision logic.
# --------------------------------------------------------------------------- #

THROWS_ON_INIT_HTML = """
<!doctype html><html><head><title>Report</title></head>
<body><main><h1>Quarterly Report</h1><div id="total">Total</div></main>
<script>document.getElementById('total').textContent = TOTAL_MISSING_CONSTANT;</script>
</body></html>
"""

UNBOUND_SCROLL_HTML = """
<!doctype html><html><head><title>Grid</title></head>
<body><main><div id="grid" style="height:100px;overflow:auto">
<div style="height:9000px"><div class="row">Row 1</div></div></div></main>
<script>/* the scroll handler that would render Row 9999 is never bound */</script>
</body></html>
"""

NOOP_EXPORT_HTML = """
<!doctype html><html><head><title>Data</title></head>
<body><main><button id="export">Export</button></main>
<script>document.getElementById('export').addEventListener('click', () => {});</script>
</body></html>
"""

OVERFLOW_HTML = """
<!doctype html><html><head><title>Wide</title>
<style>body{margin:0}#wide{width:520px;height:40px;background:#eee}</style></head>
<body><main><div id="wide">too wide for a phone</div></main></body></html>
"""

FETCHING_TOGGLE_HTML = """
<!doctype html><html><head><title>Sort</title></head>
<body><main><button id="sort">Sort</button></main>
<script>document.getElementById('sort').addEventListener('click', () => {
  fetch('/api/unexpected').catch(() => {});
});</script></body></html>
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

    def test_throwing_init_fails_closed(self) -> None:
        with self.assertRaises(BrowserAcceptanceError) as caught:
            self._drive(THROWS_ON_INIT_HTML, _contract())
        self.assertEqual(caught.exception.code, "browser_acceptance.console_failure")

    def test_unbound_scroll_fails_closed(self) -> None:
        contract = _contract(
            steps=(BrowserInteractionStep("scroll", "#grid", "5000"),),
            postconditions=(
                BrowserPostcondition("row-9999", "row-identity", ".row", "Row 9999"),
            ),
        )
        with self.assertRaises(BrowserAcceptanceError) as caught:
            self._drive(UNBOUND_SCROLL_HTML, contract)
        self.assertEqual(
            caught.exception.code, "browser_acceptance.postcondition_failed"
        )

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

    def test_overflow_fails_closed_at_390(self) -> None:
        with self.assertRaises(BrowserAcceptanceError) as caught:
            self._drive(OVERFLOW_HTML, _contract(viewports=(MOBILE,)))
        self.assertEqual(caught.exception.code, "browser_acceptance.document_overflow")

    def test_fetching_toggle_fails_request_count(self) -> None:
        contract = _contract(
            steps=(BrowserInteractionStep("click", "#sort"),),
            expected_request_count=0,
        )
        with self.assertRaises(BrowserAcceptanceError) as caught:
            self._drive(FETCHING_TOGGLE_HTML, contract)
        self.assertEqual(caught.exception.code, "browser_acceptance.request_count")

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
