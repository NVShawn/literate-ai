"""Pinned test dispatch: closure-aware skip, admission scheduling, receipt tags."""

from __future__ import annotations

import unittest

from literate_ai.application.pinned_test_dispatch import (
    DispatchAction,
    MemoryTestedTagStore,
    Pin,
    PinDispatchService,
    PinKind,
    pin_closure_identity,
    pin_plan_identity,
)
from literate_ai.contracts import (
    ContentIdentity,
    ProjectTestEvidence,
    ProjectTestReceipt,
    ProjectTestSummary,
    VersionedContentRef,
    canonical_identity,
)


def _identity(label: str) -> ContentIdentity:
    return canonical_identity({"fixture": label})


def _receipt(subject: ContentIdentity) -> ProjectTestReceipt:
    return ProjectTestReceipt(
        project_id="example-project",
        project_revision_identity=_identity("project-authority"),
        subject_identity=subject,
        suite=VersionedContentRef(
            "test-suite",
            "portable-e2e",
            "1.0.0",
            _identity("suite"),
        ),
        outcome="passed",
        summary=ProjectTestSummary(total=3, passed=3, failed=0, skipped=0),
        result_identity=_identity("normalized-test-result"),
        evidence=(
            ProjectTestEvidence("build-result", _identity("build")),
            ProjectTestEvidence("lifecycle-command", _identity("lifecycle-command")),
            ProjectTestEvidence("lifecycle-request", _identity("lifecycle-request")),
            ProjectTestEvidence("observation-result", _identity("runtime")),
            ProjectTestEvidence(
                "source-cache-decision", _identity("source-cache-decision")
            ),
            ProjectTestEvidence(
                "source-cache-lifecycle", _identity("source-cache-lifecycle")
            ),
        ),
    )


def _component_pin(
    coordinate: str,
    *,
    own: ContentIdentity,
    dependencies: tuple[ContentIdentity, ...] = (),
    declared_inputs: tuple[str, ...] | None = None,
    pipeline: tuple[str, ...] = ("generated-tests", "acceptance"),
) -> Pin:
    identity = pin_closure_identity(
        kind=PinKind.COMPONENT,
        coordinate=coordinate,
        own_identity=own,
        dependencies=dependencies,
    )
    return Pin(
        identity=identity,
        kind=PinKind.COMPONENT,
        coordinate=coordinate,
        pipeline=pipeline,
        declared_inputs=declared_inputs or (coordinate,),
        own_identity=own,
    )


def _bound_store(*pins: Pin) -> MemoryTestedTagStore:
    """A tested-tag store where each pin's receipt is bound to its own plan.

    Mirrors what ``PinDispatchService.record_tested`` produces after a real
    passing run, as opposed to the legacy/unbound construction path exercised
    by ``MemoryTestedTagStore(receipts)`` directly.
    """

    store = MemoryTestedTagStore()
    for pin in pins:
        store.record(pin.identity, pin_plan_identity(pin), _receipt(pin.identity))
    return store


class _RecordingOpener:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []

    def open(self, pin: Pin, input_label: str) -> bytes:
        self.opened.append((pin.coordinate, input_label))
        return b""


class PinnedTestDispatchTests(unittest.TestCase):
    def test_closure_change_reruns_only_the_changed_pin(self) -> None:
        own_a = _identity("component-a-own")
        own_b = _identity("component-b-own")
        shared = _identity("shared-dep-v1")
        other = _identity("b-only-dep")
        pin_a = _component_pin(
            "component://example/a", own=own_a, dependencies=(shared,)
        )
        pin_b = _component_pin(
            "component://example/b", own=own_b, dependencies=(other,)
        )
        service = PinDispatchService(receipts=_bound_store(pin_a, pin_b))
        opener = _RecordingOpener()
        first = service.evaluate((pin_a, pin_b), open_inputs=opener)
        self.assertEqual([item.action for item in first], [DispatchAction.SKIP] * 2)
        self.assertEqual(opener.opened, [])

        changed = _identity("shared-dep-v2")
        pin_a_next = _component_pin(
            "component://example/a", own=own_a, dependencies=(changed,)
        )
        self.assertEqual(pin_a_next.own_identity, pin_a.own_identity)
        self.assertNotEqual(pin_a_next.identity, pin_a.identity)
        opener = _RecordingOpener()
        second = service.evaluate((pin_a_next, pin_b), open_inputs=opener)
        self.assertEqual(second[0].action, DispatchAction.RUN)
        self.assertEqual(second[1].action, DispatchAction.SKIP)
        self.assertEqual(
            opener.opened,
            [("component://example/a", "component://example/a")],
        )
        self.assertNotIn("component://example/b", [item[0] for item in opener.opened])

    def test_cross_pin_receipt_never_matches_a_different_pin(self) -> None:
        pin_a = _component_pin("component://example/x", own=_identity("x-own"))
        pin_b = _component_pin("component://example/y", own=_identity("y-own"))
        service = PinDispatchService(receipts=_bound_store(pin_a))
        opener = _RecordingOpener()
        scheduled = service.evaluate((pin_b,), open_inputs=opener)
        self.assertEqual(scheduled[0].action, DispatchAction.RUN)
        self.assertTrue(opener.opened)
