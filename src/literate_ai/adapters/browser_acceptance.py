"""A browser-tool-neutral acceptance port and the fail-closed decision logic.

ADR 0028 decides a verifier-owned browser acceptance mode for a served frontend
deployment unit (ADR-0026), distinct from the Component's own generated tests. A
generated frontend repeatedly passed text-only persistent-service acceptance while
shipping observable failures -- ``NaN`` rendered, inline scripts referencing
undefined constants, a virtualized table that computed the right window in unit
tests but bound no working scroll handler, export controls that were no-op
handlers, and persistent mobile document overflow. Text-only acceptance checks the
HTTP response body, so none of those fail closed. The FRONTEND-002
``verify-frontend-browser`` operator skill (``skills/agent/verify-frontend-browser``)
is the behavioral checklist a reviewer runs by hand; this module is the
verifier-enforcement that closes the gap that skill left open (#218).

This is deliberately **not** Playwright-shaped. The contract declares viewports,
interaction steps, observable postconditions, and rejection classes; the port
below is a small infrastructure boundary (ADR-0004 executable-port boundaries)
whose whole surface is "launch a page at a viewport, drive the declared steps,
report what the browser observed." Playwright is the first adapter but the
declared contract never mentions it, so a second engine can be added without
rewriting acceptance specs.

The two halves are separable on purpose:

* ``BrowserPageDriver`` is the port -- an abstract class an engine implements. A
  driver returns a plain ``BrowserObservation`` (console errors, page errors,
  failed requests, HTTP-error responses, document overflow, request count,
  resolved postconditions, screenshot + accessibility snapshot). It makes *no*
  pass/fail decision.
* ``decide_browser_acceptance`` is the pure decision. Given a
  ``BrowserInteractionAcceptance`` contract and a ``BrowserObservation`` it
  either returns an evidence document or raises ``BrowserAcceptanceError``. It
  imports no browser engine, so the fail-closed logic (given a browser
  observation, does it pass or fail?) is fully testable with a fake driver and
  no real browser.

Playwright is an acceptance-time optional dependency. ``PlaywrightPageDriver``
imports it lazily inside the method that needs it, so importing this module
without Playwright installed does not crash, and a missing browser engine yields
a clear typed :class:`BrowserAcceptanceError` (fail closed, never a silent skip).
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from literate_ai.contracts import ContentIdentity, canonical_identity

if TYPE_CHECKING:
    from literate_ai.adapters.component_acceptance import (
        BrowserInteractionAcceptance,
        BrowserPostcondition,
        BrowserViewport,
    )


BROWSER_ACCEPTANCE_EVIDENCE_SCHEMA = "literate-ai/browser-interaction-observation@1"


class BrowserAcceptanceError(RuntimeError):
    """A browser observation that fails the verifier-owned contract, or an
    unavailable browser engine. Either way acceptance fails closed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class BrowserConsoleFailure:
    """One console error, uncaught page error, failed request, or HTTP-error
    response the browser surfaced -- each an ADR-0028 rejection class."""

    kind: str
    text: str
    location: str = ""

    def to_document(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "location": self.location}


@dataclass(frozen=True, slots=True)
class BrowserObservation:
    """What a driver observed after launching a page and running the steps.

    This is the neutral hand-off between any engine and the pure decision. A
    driver never decides pass/fail; it reports facts. ``resolved_postconditions``
    maps a postcondition id to whether the driver observed it satisfied.
    """

    viewport_width: int
    viewport_height: int
    document_scroll_width: int
    console_failures: tuple[BrowserConsoleFailure, ...] = ()
    request_count: int = 0
    resolved_postconditions: tuple[tuple[str, bool], ...] = ()
    observed_roles: tuple[tuple[str, str], ...] = ()
    observed_landmarks: tuple[str, ...] = ()
    screenshot: bytes = b""
    accessibility_snapshot: bytes = b""

    def postcondition(self, postcondition_id: str) -> bool | None:
        for identifier, satisfied in self.resolved_postconditions:
            if identifier == postcondition_id:
                return satisfied
        return None

    def has_role(self, role: str, name: str) -> bool:
        return any(
            observed_role == role and observed_name == name
            for observed_role, observed_name in self.observed_roles
        )


class BrowserPageDriver(ABC):
    """The browser-tool-neutral port an engine implements (ADR-0004 boundary).

    A driver launches the frontend at ``base_url`` at one viewport, runs the
    declared interaction steps, and returns a :class:`BrowserObservation`. It
    raises :class:`BrowserAcceptanceError` only for an unavailable engine or an
    unrunnable step -- pass/fail against the contract is decided elsewhere.
    """

    @property
    @abstractmethod
    def tool_identity(self) -> ContentIdentity:
        """Identity of the driving engine, bound into the evidence receipt."""

    @abstractmethod
    def observe(
        self,
        base_url: str,
        viewport: BrowserViewport,
        contract: BrowserInteractionAcceptance,
    ) -> BrowserObservation:
        """Drive the page at one viewport and report what the browser observed."""


def _bytes_identity(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _postcondition_holds(
    observation: BrowserObservation, postcondition: BrowserPostcondition
) -> bool:
    resolved = observation.postcondition(postcondition.postcondition_id)
    if resolved is None:
        return False
    return bool(resolved)


def decide_browser_acceptance(
    contract: BrowserInteractionAcceptance,
    viewport: BrowserViewport,
    observation: BrowserObservation,
) -> dict[str, Any]:
    """Fail closed on any rejected observation; otherwise return evidence.

    This is the entire acceptance decision, engine-free. It rejects, in order,
    every ADR-0028 rejection class the contract enables: browser console errors,
    page errors, failed requests, and HTTP-error responses; horizontal document
    overflow past the viewport; missing accessible roles/names and landmarks;
    unsatisfied observable postconditions; and a request count that differs from
    a declared local-only expectation. A pass is evidence for exactly this
    viewport + this contract + this driver, nothing more.
    """

    failures = [
        failure
        for failure in observation.console_failures
        if failure.kind in contract.rejection_classes
    ]
    if failures:
        raise BrowserAcceptanceError(
            "browser_acceptance.console_failure",
            "browser surfaced a rejected observation: "
            + "; ".join(f"{item.kind}: {item.text}" for item in failures[:8]),
        )
    if (
        contract.reject_horizontal_overflow
        and observation.document_scroll_width > viewport.width
    ):
        raise BrowserAcceptanceError(
            "browser_acceptance.document_overflow",
            f"document scrollWidth {observation.document_scroll_width} exceeds the "
            f"{viewport.width}px viewport ({viewport.label})",
        )
    for role, name in contract.required_roles:
        if not observation.has_role(role, name):
            raise BrowserAcceptanceError(
                "browser_acceptance.role_missing",
                f"required accessible role {role!r} named {name!r} was not "
                f"observed at {viewport.label}",
            )
    for landmark in contract.required_landmarks:
        if landmark not in observation.observed_landmarks:
            raise BrowserAcceptanceError(
                "browser_acceptance.landmark_missing",
                f"required landmark {landmark!r} was not observed at {viewport.label}",
            )
    for postcondition in contract.postconditions:
        if not _postcondition_holds(observation, postcondition):
            raise BrowserAcceptanceError(
                "browser_acceptance.postcondition_failed",
                f"postcondition {postcondition.postcondition_id!r} "
                f"({postcondition.kind}) was not observed at {viewport.label}",
            )
    if (
        contract.expected_request_count is not None
        and observation.request_count != contract.expected_request_count
    ):
        raise BrowserAcceptanceError(
            "browser_acceptance.request_count",
            f"observed {observation.request_count} requests but the contract "
            f"declares {contract.expected_request_count} for local-only "
            f"interactions at {viewport.label}",
        )
    return {
        "viewport": {
            "label": viewport.label,
            "width": viewport.width,
            "height": viewport.height,
        },
        "document_scroll_width": observation.document_scroll_width,
        "request_count": observation.request_count,
        "console_failures": [
            item.to_document() for item in observation.console_failures
        ],
        "observed_landmarks": list(observation.observed_landmarks),
        "resolved_postconditions": [
            {"postcondition_id": identifier, "satisfied": satisfied}
            for identifier, satisfied in observation.resolved_postconditions
        ],
        "screenshot_identity": _bytes_identity(observation.screenshot),
        "accessibility_snapshot_identity": _bytes_identity(
            observation.accessibility_snapshot
        ),
        "outcome": "accepted",
    }


class PlaywrightPageDriver(BrowserPageDriver):
    """Playwright-backed implementation of the neutral port (first adapter).

    Playwright is imported lazily inside :meth:`observe`, so importing this
    module without Playwright installed does not crash. A missing engine or a
    browser that cannot launch raises :class:`BrowserAcceptanceError` -- the
    verifier fails closed rather than silently skipping browser acceptance.
    """

    def __init__(self, *, headless: bool = True) -> None:
        self._headless = headless

    @property
    def tool_identity(self) -> ContentIdentity:
        version = self._playwright_version()
        return canonical_identity(
            {
                "schema": "literate-ai/browser-driver@1",
                "engine": "playwright-chromium",
                "playwright_version": version,
                "headless": self._headless,
            }
        )

    @staticmethod
    def _playwright_version() -> str:
        try:
            from importlib.metadata import version

            return version("playwright")
        except Exception:  # pragma: no cover - metadata absent
            return "unknown"

    def observe(
        self,
        base_url: str,
        viewport: BrowserViewport,
        contract: BrowserInteractionAcceptance,
    ) -> BrowserObservation:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserAcceptanceError(
                "browser_acceptance.engine_unavailable",
                "Playwright is not installed; browser acceptance cannot run. "
                "Install the 'browser' extra to enable it.",
            ) from exc

        console_failures: list[BrowserConsoleFailure] = []
        request_count = 0
        try:
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=self._headless)
                except Exception as exc:  # engine present, browser missing
                    raise BrowserAcceptanceError(
                        "browser_acceptance.engine_unavailable",
                        "Chromium could not be launched; run "
                        "'playwright install chromium'.",
                    ) from exc
                try:
                    context = browser.new_context(
                        viewport={"width": viewport.width, "height": viewport.height}
                    )
                    page = context.new_page()

                    def on_console(message: Any) -> None:
                        if message.type == "error":
                            console_failures.append(
                                BrowserConsoleFailure(
                                    "console-error",
                                    str(message.text),
                                    str(getattr(message, "location", "") or ""),
                                )
                            )

                    def on_page_error(error: Any) -> None:
                        console_failures.append(
                            BrowserConsoleFailure("page-error", str(error))
                        )

                    def on_request_failed(request: Any) -> None:
                        console_failures.append(
                            BrowserConsoleFailure(
                                "failed-request",
                                f"{request.method} {request.url}",
                            )
                        )

                    def on_response(response: Any) -> None:
                        nonlocal request_count
                        request_count += 1
                        if response.status >= 400:
                            console_failures.append(
                                BrowserConsoleFailure(
                                    "http-error",
                                    f"{response.status} {response.url}",
                                )
                            )

                    page.on("console", on_console)
                    page.on("pageerror", on_page_error)
                    page.on("requestfailed", on_request_failed)
                    page.on("response", on_response)

                    page.goto(base_url, wait_until="load")
                    # Count only interaction-driven requests: reset after the
                    # initial navigation so a local sort/toggle that fetches
                    # unexpectedly is caught by the request-count assertion.
                    interaction_requests = {"count": 0}

                    def count_response(_response: Any) -> None:
                        interaction_requests["count"] += 1

                    page.on("response", lambda r: count_response(r))
                    observation = self._drive(
                        page, viewport, contract, interaction_requests
                    )
                    return BrowserObservation(
                        viewport_width=viewport.width,
                        viewport_height=viewport.height,
                        document_scroll_width=observation["scroll_width"],
                        console_failures=tuple(console_failures),
                        request_count=observation["request_count"],
                        resolved_postconditions=observation["postconditions"],
                        observed_roles=observation["roles"],
                        observed_landmarks=observation["landmarks"],
                        screenshot=observation["screenshot"],
                        accessibility_snapshot=observation["snapshot"],
                    )
                finally:
                    browser.close()
        except BrowserAcceptanceError:
            raise
        except Exception as exc:
            raise BrowserAcceptanceError(
                "browser_acceptance.driver_failed",
                f"browser driver failed while driving {viewport.label}: {exc}",
            ) from exc

    def _drive(
        self,
        page: Any,
        viewport: BrowserViewport,
        contract: BrowserInteractionAcceptance,
        interaction_requests: dict[str, int],
    ) -> dict[str, Any]:
        import json as _json

        for step in contract.steps:
            self._run_step(page, step)
        # Let any interaction-driven fetch settle so its response is counted; a
        # local-only interaction issues none and this is a bounded idle wait.
        try:
            page.wait_for_timeout(150)
        except Exception:
            pass

        scroll_width = int(page.evaluate("() => document.documentElement.scrollWidth"))
        roles: list[tuple[str, str]] = []
        for role, name in contract.required_roles:
            try:
                located = page.get_by_role(role, name=name)
                if located.count() > 0:
                    roles.append((role, name))
            except Exception:
                continue
        landmarks: list[str] = []
        for landmark in contract.required_landmarks:
            try:
                if page.locator(landmark).count() > 0:
                    landmarks.append(landmark)
            except Exception:
                continue
        postconditions: list[tuple[str, bool]] = []
        for postcondition in contract.postconditions:
            postconditions.append(
                (
                    postcondition.postcondition_id,
                    self._check_postcondition(page, postcondition),
                )
            )
        try:
            screenshot = page.screenshot(full_page=True)
        except Exception:
            screenshot = b""
        try:
            snapshot = _json.dumps(page.accessibility.snapshot() or {}).encode("utf-8")
        except Exception:
            snapshot = b""
        return {
            "scroll_width": scroll_width,
            "request_count": interaction_requests["count"],
            "roles": tuple(roles),
            "landmarks": tuple(landmarks),
            "postconditions": tuple(postconditions),
            "screenshot": screenshot,
            "snapshot": snapshot,
        }

    @staticmethod
    def _run_step(page: Any, step: Any) -> None:
        action = step.action
        selector = step.selector
        if action == "click":
            page.locator(selector).first.click()
        elif action == "fill":
            page.locator(selector).first.fill(step.value)
        elif action == "select":
            page.locator(selector).first.select_option(step.value)
        elif action == "scroll":
            page.locator(selector).first.evaluate(
                "(node, amount) => { node.scrollTop = amount; "
                "node.dispatchEvent(new Event('scroll')); }",
                int(step.value or "0"),
            )
        elif action == "keyboard":
            page.locator(selector).first.press(step.value)

    @staticmethod
    def _check_postcondition(page: Any, postcondition: Any) -> bool:
        kind = postcondition.kind
        selector = postcondition.selector
        expected = postcondition.expected
        try:
            if kind == "text":
                return expected in (page.locator(selector).first.inner_text() or "")
            if kind == "count":
                return page.locator(selector).count() == int(expected)
            if kind == "aria":
                return (
                    page.locator(selector).first.get_attribute(
                        postcondition.attribute or "aria-label"
                    )
                    == expected
                )
            if kind == "state":
                attribute = postcondition.attribute or "checked"
                return bool(
                    page.locator(selector).first.evaluate(
                        f"(node) => Boolean(node.{attribute})"
                    )
                )
            if kind == "row-identity":
                texts = page.locator(selector).all_inner_texts()
                return expected in texts
            if kind == "download":
                with page.expect_download(timeout=5000) as info:
                    page.locator(selector).first.click()
                download = info.value
                return bool(download.suggested_filename)
        except Exception:
            return False
        return False


__all__ = [
    "BROWSER_ACCEPTANCE_EVIDENCE_SCHEMA",
    "BrowserAcceptanceError",
    "BrowserConsoleFailure",
    "BrowserObservation",
    "BrowserPageDriver",
    "PlaywrightPageDriver",
    "decide_browser_acceptance",
]
