"""Pinned test dispatch: closure-aware skip, admission scheduling, receipt tags."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from literate_ai.application.pinned_test_dispatch import (
    TESTED_AT_PIN_PROPERTY,
    AdmissionEvent,
    DispatchAction,
    MemoryMarkerStore,
    MemoryTestedTagStore,
    Pin,
    PinDispatchError,
    PinDispatchService,
    PinKind,
    PinMarker,
    bind_gates_to_pins,
    pin_closure_identity,
    pin_from_component_entry,
    pin_from_documentation_authority,
    pin_from_flavor_entry,
    pin_from_framework_tcb,
    pin_from_skill_entry,
    pin_plan_identity,
    pins_from_authority_inventory,
    rekey_receipt_to_pin,
    sbom_properties_for_tested_pin,
    suite_fallback_pin,
)
from literate_ai.application.pinned_test_dispatch import (
    tested_pin_from_sbom_properties as read_tested_pin_from_sbom_properties,
)
from literate_ai.application.pinned_test_dispatch import (
    tested_tag_for_pin as create_tested_tag_for_pin,
)
from literate_ai.application.project_authority import (
    ComponentAuthorityReviewEntry,
    FlavorAuthorityReviewEntry,
    ForwardSkillAuthorityReviewEntry,
    ProjectAuthorityInventory,
)
from literate_ai.contracts import (
    ContentIdentity,
    ProjectTestEvidence,
    ProjectTestReceipt,
    ProjectTestSummary,
    VersionedContentRef,
    canonical_identity,
)
from literate_ai.test_checkpointing import run_gates, run_unittest_suite


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

    def test_tested_pin_skips_without_reading_declared_inputs(self) -> None:
        pin = _component_pin("component://example/lib", own=_identity("lib-own"))
        service = PinDispatchService(receipts=_bound_store(pin))
        opener = _RecordingOpener()

        scheduled = service.evaluate((pin,), open_inputs=opener)
        self.assertEqual(scheduled[0].action, DispatchAction.SKIP)
        self.assertEqual(scheduled[0].receipt, _receipt(pin.identity))
        self.assertEqual(opener.opened, [])

    def test_inserted_deleted_or_reordered_gate_forces_evaluation(self) -> None:
        # TESTED-PIN-PLAN-001 (#319): a durable receipt must attest both the
        # content-pin identity AND the current ordered plan. The own/content
        # identity below is held fixed across every case; only the pipeline
        # (ordered gate/test plan) changes, so any resulting RUN can only be
        # explained by the plan-identity check, not by pin identity drift.
        own = _identity("plan-fixed-own")
        original = _component_pin(
            "component://example/plan",
            own=own,
            pipeline=("generated-tests", "acceptance"),
        )
        service = PinDispatchService(receipts=_bound_store(original))

        same_plan = _component_pin(
            "component://example/plan",
            own=own,
            pipeline=("generated-tests", "acceptance"),
        )
        self.assertEqual(same_plan.identity, original.identity)
        skip_opener = _RecordingOpener()
        skipped = service.evaluate((same_plan,), open_inputs=skip_opener)
        self.assertEqual(skipped[0].action, DispatchAction.SKIP)
        self.assertEqual(skip_opener.opened, [])

        inserted = _component_pin(
            "component://example/plan",
            own=own,
            pipeline=("generated-tests", "new-gate", "acceptance"),
        )
        self.assertEqual(inserted.identity, original.identity)
        run_opener = _RecordingOpener()
        result = service.evaluate((inserted,), open_inputs=run_opener)
        self.assertEqual(result[0].action, DispatchAction.RUN)
        self.assertTrue(run_opener.opened)

        deleted = _component_pin(
            "component://example/plan", own=own, pipeline=("generated-tests",)
        )
        self.assertEqual(deleted.identity, original.identity)
        run_opener = _RecordingOpener()
        result = service.evaluate((deleted,), open_inputs=run_opener)
        self.assertEqual(result[0].action, DispatchAction.RUN)
        self.assertTrue(run_opener.opened)

        reordered = _component_pin(
            "component://example/plan",
            own=own,
            pipeline=("acceptance", "generated-tests"),
        )
        self.assertEqual(reordered.identity, original.identity)
        run_opener = _RecordingOpener()
        result = service.evaluate((reordered,), open_inputs=run_opener)
        self.assertEqual(result[0].action, DispatchAction.RUN)
        self.assertTrue(run_opener.opened)

    def test_legacy_unbound_receipt_fails_closed(self) -> None:
        # A receipt constructed via the legacy path (no explicit plan binding,
        # e.g. the durable Git receipt loaded from disk before this fix) must
        # never be silently treated as attesting the current plan.
        pin = _component_pin("component://example/legacy", own=_identity("legacy-own"))
        service = PinDispatchService(
            receipts=MemoryTestedTagStore((_receipt(pin.identity),))
        )
        opener = _RecordingOpener()
        scheduled = service.evaluate((pin,), open_inputs=opener)
        self.assertEqual(scheduled[0].action, DispatchAction.RUN)
        self.assertTrue(opener.opened)

        # Explicitly recording the same receipt through the plan-bound API
        # migrates it, and it can then skip on an exact (pin, plan) match.
        service.record_tested(pin, _receipt(pin.identity))
        opener = _RecordingOpener()
        scheduled = service.evaluate((pin,), open_inputs=opener)
        self.assertEqual(scheduled[0].action, DispatchAction.SKIP)
        self.assertEqual(opener.opened, [])

    def test_cross_pin_receipt_never_matches_a_different_pin(self) -> None:
        pin_a = _component_pin("component://example/x", own=_identity("x-own"))
        pin_b = _component_pin("component://example/y", own=_identity("y-own"))
        service = PinDispatchService(receipts=_bound_store(pin_a))
        opener = _RecordingOpener()
        scheduled = service.evaluate((pin_b,), open_inputs=opener)
        self.assertEqual(scheduled[0].action, DispatchAction.RUN)
        self.assertTrue(opener.opened)

    def test_unadmitted_artifact_never_enters_the_scheduler(self) -> None:
        derived = _component_pin(
            "component://example/derived", own=_identity("derived")
        )
        sibling = _component_pin(
            "component://example/sibling", own=_identity("sibling")
        )
        service = PinDispatchService()
        opener = _RecordingOpener()
        admission = AdmissionEvent(derived.identity, _identity("cache-007-admission"))

        scheduled = service.schedule(
            (derived, sibling),
            (admission,),
            open_inputs=opener,
        )
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0].pin.coordinate, "component://example/derived")
        self.assertEqual(scheduled[0].action, DispatchAction.RUN)
        self.assertEqual(
            opener.opened,
            [("component://example/derived", "component://example/derived")],
        )
        self.assertNotIn(
            "component://example/sibling", [item[0] for item in opener.opened]
        )

    def test_tested_tag_is_the_receipt_rekeyed_by_pin_and_sbom_property(self) -> None:
        pin = _component_pin("component://example/pkg", own=_identity("pkg-own"))
        lock_list = _receipt(_identity("ad-hoc-lock-list"))
        self.assertNotEqual(lock_list.subject_identity, pin.identity)
        keyed = rekey_receipt_to_pin(lock_list, pin)
        self.assertEqual(keyed.subject_identity, pin.identity)
        tag_store = MemoryTestedTagStore()
        service = PinDispatchService(receipts=tag_store)
        tag = service.record_tested(pin, keyed)
        self.assertEqual(tag.receipt.subject_identity, pin.identity)
        self.assertEqual(tag_store.lookup(pin.identity, pin_plan_identity(pin)), keyed)
        properties = sbom_properties_for_tested_pin(pin)
        self.assertEqual(properties[0]["name"], TESTED_AT_PIN_PROPERTY)
        self.assertEqual(read_tested_pin_from_sbom_properties(properties), pin.identity)
        with self.assertRaises(PinDispatchError) as raised:
            create_tested_tag_for_pin(pin, lock_list)
        self.assertEqual(raised.exception.code, "pin_dispatch.receipt_not_keyed_by_pin")

    def test_authority_inventory_pins_use_existing_closure_identities(self) -> None:
        closure = pin_closure_identity(
            kind=PinKind.COMPONENT,
            coordinate="component://example/root",
            own_identity=_identity("root-rev"),
            dependencies=(_identity("dep"),),
        )
        inventory = ProjectAuthorityInventory(
            project_definition=_identity("project"),
            repository_lineage=_identity("lineage"),
            onboarding_skill=_identity("skill.md"),
            components=(
                ComponentAuthorityReviewEntry(
                    coordinate="component://example/root",
                    revision=_identity("root-rev").uri,
                    workflow=_identity("workflow").uri,
                    routing=_identity("routing").uri,
                    input_closure=closure.uri,
                ),
            ),
            flavors=(
                FlavorAuthorityReviewEntry(
                    "flavor://example/lang-python",
                    _identity("flavor-rev").uri,
                    _identity("flavor-specs").uri,
                ),
            ),
            specification_to_source_skills=(
                ForwardSkillAuthorityReviewEntry(
                    "python-service",
                    "1.0.0",
                    _identity("skill-id").uri,
                ),
            ),
        )
        tcb = _identity("lifecycle-driver-tcb")
        pins = pins_from_authority_inventory(inventory, tcb_identity=tcb)
        kinds = {pin.kind: pin for pin in pins}
        self.assertEqual(kinds[PinKind.COMPONENT].identity, closure)
        self.assertEqual(kinds[PinKind.FLAVOR].pipeline, ())
        self.assertEqual(kinds[PinKind.SKILL].pipeline, ())
        self.assertEqual(kinds[PinKind.FRAMEWORK_TCB].identity, tcb)
        self.assertEqual(
            kinds[PinKind.DOCUMENTATION_AUTHORITY].kind,
            PinKind.DOCUMENTATION_AUTHORITY,
        )
        flavor = pin_from_flavor_entry(inventory.flavors[0])
        skill = pin_from_skill_entry(inventory.specification_to_source_skills[0])
        component = pin_from_component_entry(inventory.components[0])
        self.assertEqual(flavor.identity, _identity("flavor-rev"))
        self.assertEqual(skill.identity, _identity("skill-id"))
        self.assertEqual(component.own_identity, _identity("root-rev"))

    def test_release_check_consults_dispatch_not_name_only_skip(self) -> None:
        own = _identity("tcb-own")
        dep = _identity("tcb-dep-v1")
        tcb = pin_from_framework_tcb(
            pin_closure_identity(
                kind=PinKind.FRAMEWORK_TCB,
                coordinate="framework-tcb",
                own_identity=own,
                dependencies=(dep,),
            ),
            declared_inputs=("lifecycle-driver-tcb",),
        )
        docs = pin_from_documentation_authority(_identity("docs-v1"))
        gates = ("python-check", "documentation-check")
        catalog = bind_gates_to_pins(gates, tcb=tcb, docs=docs)
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "release.json"
            ran: list[str] = []

            def fail_docs(gate: str) -> int:
                ran.append(gate)
                return 7 if gate == "documentation-check" else 0

            self.assertEqual(
                run_gates(
                    suite="release-check",
                    gates=gates,
                    state_path=state,
                    runner=fail_docs,
                    gate_pins=catalog,
                ),
                7,
            )
            self.assertEqual(ran, ["python-check", "documentation-check"])

            tcb_next = pin_from_framework_tcb(
                pin_closure_identity(
                    kind=PinKind.FRAMEWORK_TCB,
                    coordinate="framework-tcb",
                    own_identity=own,
                    dependencies=(_identity("tcb-dep-v2"),),
                ),
                declared_inputs=("lifecycle-driver-tcb",),
            )
            catalog_next = bind_gates_to_pins(gates, tcb=tcb_next, docs=docs)
            ran.clear()
            self.assertEqual(
                run_gates(
                    suite="release-check",
                    gates=gates,
                    state_path=state,
                    runner=fail_docs,
                    gate_pins=catalog_next,
                ),
                7,
            )
            self.assertEqual(ran, ["python-check", "documentation-check"])

            catalog_same_docs = bind_gates_to_pins(gates, tcb=tcb, docs=docs)
            service = PinDispatchService(
                receipts=_bound_store(catalog_same_docs["python-check"])
            )
            ran.clear()
            self.assertEqual(
                run_gates(
                    suite="release-check",
                    gates=gates,
                    state_path=Path(temporary) / "fresh.json",
                    runner=lambda gate: ran.append(gate) or 0,
                    gate_pins=catalog_same_docs,
                    dispatch=service,
                ),
                0,
            )
            self.assertEqual(ran, ["documentation-check"])

    def test_unittest_runner_skips_tested_pin_without_running_cases(self) -> None:
        executions: list[str] = []

        class Cases(unittest.TestCase):
            def test_a(self) -> None:
                executions.append("a")

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Cases)
        test_ids = tuple(test.id() for test in suite)
        pin = pin_from_framework_tcb(_identity("python-tcb"), pipeline=test_ids)
        service = PinDispatchService(receipts=_bound_store(pin))
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "python.json"
            self.assertEqual(
                run_unittest_suite(
                    suite,
                    state_path=state,
                    stream=io.StringIO(),
                    pin=pin,
                    dispatch=service,
                ),
                0,
            )
        self.assertEqual(executions, [])

    def test_marker_resume_does_not_reuse_fingerprint_or_name_only_state(self) -> None:
        pin = _component_pin("component://example/svc", own=_identity("svc"))
        service = PinDispatchService(
            markers=MemoryMarkerStore((PinMarker(pin.identity, ("generated-tests",)),))
        )
        opener = _RecordingOpener()
        scheduled = service.evaluate((pin,), open_inputs=opener)
        self.assertEqual(scheduled[0].action, DispatchAction.RESUME)
        self.assertEqual(scheduled[0].completed_steps, ("generated-tests",))
        self.assertEqual(
            opener.opened,
            [("component://example/svc", "component://example/svc")],
        )
        service.record_step(pin, "acceptance")
        opener = _RecordingOpener()
        done = service.evaluate((pin,), open_inputs=opener)
        self.assertEqual(done[0].action, DispatchAction.RESUME)
        self.assertEqual(done[0].completed_steps, ("generated-tests", "acceptance"))
        self.assertEqual(opener.opened, [])

    def test_suite_fallback_pin_is_stable_for_isolated_runners(self) -> None:
        first = suite_fallback_pin("release-check", ("fast", "expensive"))
        second = suite_fallback_pin("release-check", ("fast", "expensive", "final"))
        self.assertEqual(first.identity, second.identity)
        self.assertNotEqual(first.pipeline, second.pipeline)


if __name__ == "__main__":
    unittest.main()
