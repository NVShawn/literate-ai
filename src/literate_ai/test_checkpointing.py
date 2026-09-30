"""Portable fail-fast repair checkpoints for framework and derived-project tests."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TextIO

from literate_ai.application.pinned_test_dispatch import (
    DispatchAction,
    MemoryTestedTagStore,
    Pin,
    PinDispatchService,
    PinMarker,
    bind_gates_to_pins,
    pin_from_documentation_authority,
    pin_from_framework_tcb,
    suite_fallback_pin,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.known_test_failures import (
    KnownTestFailureAnnotation,
    KnownTestFailureOutcome,
    KnownTestFailureReport,
    KnownTestFailureSummary,
    TestOutcomeStatus,
)
from literate_ai.contracts.testing import ProjectTestReceipt
from literate_ai.diagnostics import report_progress
from literate_ai.evidence_ledger import (
    EvidenceNode,
    EvidenceRun,
    attach_run,
    open_run,
    record_subprocess,
)

SCHEMA_VERSION = 1
PYTHON_SCHEMA_VERSION = 4
PIN_CHECKPOINT_VERSION = PYTHON_SCHEMA_VERSION
GateRunner = Callable[..., int]


def _run_gate(runner: GateRunner, gate: str, node: EvidenceNode | None) -> int:
    if node is not None:
        try:
            parameters = inspect.signature(runner).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "evidence_node" in parameters:
            return runner(gate, evidence_node=node)
    return runner(gate)


def _atomic_write_json(state_path: Path, value: dict[str, object]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{state_path.name}.", dir=state_path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, state_path)
    finally:
        temporary.unlink(missing_ok=True)


class CheckpointMarkerStore:
    """Local repair markers keyed by pin hash, not by gate or test-file name."""

    def __init__(self, state_path: Path, suite: str) -> None:
        self.state_path = state_path
        self.suite = suite
        (
            loaded,
            node_ids,
            completion_order,
            annotations,
            revalidate,
            last_report,
        ) = _load_pin_checkpoint(state_path, suite)
        self._markers = loaded
        self.node_ids = node_ids
        self.completion_order = list(completion_order)
        self.annotations = {
            (item.pin_identity.uri, item.test_key): item for item in annotations
        }
        self.revalidate = set(revalidate)
        self.last_report = last_report

    def load(self, pin_identity: ContentIdentity) -> PinMarker | None:
        return self._markers.get(pin_identity.uri)

    def save(self, marker: PinMarker) -> None:
        previous = self._markers.get(marker.pin_identity.uri)
        previous_steps = () if previous is None else previous.completed_steps
        for step in marker.completed_steps:
            if step not in previous_steps and step not in self.completion_order:
                self.completion_order.append(step)
        self._markers[marker.pin_identity.uri] = marker
        self._store()

    def _store(self) -> None:
        _store_pin_checkpoint(
            self.state_path,
            self.suite,
            self._markers,
            self.node_ids,
            self.completion_order,
            tuple(self.annotations.values()),
            frozenset(self.revalidate),
            self.last_report,
        )

    def annotate(self, annotation: KnownTestFailureAnnotation) -> None:
        self.annotations[(annotation.pin_identity.uri, annotation.test_key)] = (
            annotation
        )
        self.revalidate.discard(annotation.identity.uri)
        self._store()

    def clear(self, pin_identity: ContentIdentity, test_key: str) -> bool:
        annotation = self.annotations.pop((pin_identity.uri, test_key), None)
        if annotation is None:
            return False
        self.revalidate.discard(annotation.identity.uri)
        self._store()
        return True

    def request_revalidation(
        self, pin_identity: ContentIdentity, test_key: str
    ) -> KnownTestFailureAnnotation | None:
        annotation = self.annotations.get((pin_identity.uri, test_key))
        if annotation is None:
            return None
        self.revalidate.add(annotation.identity.uri)
        self._store()
        return annotation

    def record_report(self, report: KnownTestFailureReport) -> None:
        if report.suite != self.suite:
            raise ValueError("known-failure report suite does not match checkpoint")
        self.last_report = report
        self._store()

    def repair_progress_matches_gate_prefix(
        self, gates: Sequence[str], gate_pins: Mapping[str, Pin]
    ) -> bool:
        """Accept repair state only when it is the exact current fail-fast prefix."""

        completed = tuple(self.completion_order)
        marker_steps = tuple(
            step for marker in self._markers.values() for step in marker.completed_steps
        )
        if len(completed) > len(gates) or completed != tuple(gates[: len(completed)]):
            return False
        if Counter(completed) != Counter(marker_steps):
            return False
        active_uris = {gate_pins[gate].identity.uri for gate in gates}
        if any(uri not in active_uris for uri in self._markers):
            return False
        for uri, marker in self._markers.items():
            expected = tuple(
                gate for gate in completed if gate_pins[gate].identity.uri == uri
            )
            if marker.completed_steps != expected:
                return False
        return set(self.node_ids).issubset(completed)

    def reset_repair_progress(self) -> None:
        """Start the next run at test one without discarding retained test debt."""

        self._markers.clear()
        self.node_ids.clear()
        self.completion_order.clear()
        if not self.annotations:
            reset_checkpoint(self.state_path)
            return
        self._store()

    def matching_annotation(
        self,
        pin_identity: ContentIdentity,
        test_key: str,
        context_identity: ContentIdentity | None,
        *,
        now: datetime | None = None,
    ) -> KnownTestFailureAnnotation | None:
        annotation = self.annotations.get((pin_identity.uri, test_key))
        if annotation is None or annotation.identity.uri in self.revalidate:
            return None
        return (
            annotation
            if annotation.matches(
                pin_identity=pin_identity,
                test_key=test_key,
                context_identity=context_identity,
                now=now,
            )
            else None
        )

    def clear_successful_annotation(
        self,
        pin_identity: ContentIdentity,
        test_key: str,
        context_identity: ContentIdentity | None,
        *,
        release_evidence: bool = False,
        now: datetime | None = None,
    ) -> bool:
        annotation = self.annotations.get((pin_identity.uri, test_key))
        if annotation is None:
            return False
        if annotation.identity.uri in self.revalidate or (
            release_evidence
            and annotation.matches(
                pin_identity=pin_identity,
                test_key=test_key,
                context_identity=context_identity,
                now=now,
            )
        ):
            return self.clear(pin_identity, test_key)
        return False


def _load_pin_checkpoint(
    state_path: Path, suite: str
) -> tuple[
    dict[str, PinMarker],
    dict[str, str],
    tuple[str, ...],
    tuple[KnownTestFailureAnnotation, ...],
    frozenset[str],
    KnownTestFailureReport | None,
]:
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}, {}, (), (), frozenset(), None
    if (
        not isinstance(value, dict)
        or value.get("v") != PIN_CHECKPOINT_VERSION
        or value.get("s") != suite
        or not set(value).issubset(
            {
                "v",
                "s",
                "pins",
                "nodes",
                "completion_order",
                "known_failures",
                "revalidate",
                "last_report",
            }
        )
    ):
        return {}, {}, (), (), frozenset(), None
    raw_pins = value.get("pins")
    if not isinstance(raw_pins, dict):
        return {}, {}, (), (), frozenset(), None
    markers: dict[str, PinMarker] = {}
    for uri, payload in raw_pins.items():
        if not isinstance(uri, str) or not isinstance(payload, dict):
            return {}, {}, (), (), frozenset(), None
        completed = payload.get("ok")
        if not isinstance(completed, list) or any(
            not isinstance(item, str) for item in completed
        ):
            return {}, {}, (), (), frozenset(), None
        try:
            identity = ContentIdentity.parse_uri(uri)
        except (TypeError, ValueError):
            return {}, {}, (), (), frozenset(), None
        markers[uri] = PinMarker(identity, tuple(completed))
    node_ids = value.get("nodes", {})
    if not isinstance(node_ids, dict):
        return {}, {}, (), (), frozenset(), None
    nodes = {
        key: node_id
        for key, node_id in node_ids.items()
        if isinstance(key, str) and isinstance(node_id, str)
    }
    raw_completion_order = value.get("completion_order", [])
    if not isinstance(raw_completion_order, list) or any(
        not isinstance(item, str) for item in raw_completion_order
    ):
        return {}, {}, (), (), frozenset(), None
    completion_order = tuple(raw_completion_order)
    raw_annotations = value.get("known_failures", [])
    raw_revalidate = value.get("revalidate", [])
    if (
        not isinstance(raw_annotations, list)
        or not isinstance(raw_revalidate, list)
        or any(not isinstance(item, str) for item in raw_revalidate)
    ):
        return {}, {}, (), (), frozenset(), None
    try:
        annotations = tuple(
            KnownTestFailureAnnotation.from_dict(
                item, path=f"checkpoint.known_failures[{index}]"
            )
            for index, item in enumerate(raw_annotations)
        )
    except (TypeError, ValueError):
        return {}, {}, (), (), frozenset(), None
    keys = [(item.pin_identity.uri, item.test_key) for item in annotations]
    identities = {item.identity.uri for item in annotations}
    if len(keys) != len(set(keys)) or not set(raw_revalidate).issubset(identities):
        return {}, {}, (), (), frozenset(), None
    raw_report = value.get("last_report")
    try:
        last_report = (
            None
            if raw_report is None
            else KnownTestFailureReport.from_dict(
                raw_report, path="checkpoint.last_report"
            )
        )
    except (TypeError, ValueError):
        return {}, {}, (), (), frozenset(), None
    if last_report is not None and last_report.suite != suite:
        return {}, {}, (), (), frozenset(), None
    return (
        markers,
        nodes,
        completion_order,
        annotations,
        frozenset(raw_revalidate),
        last_report,
    )


def _store_pin_checkpoint(
    state_path: Path,
    suite: str,
    markers: Mapping[str, PinMarker],
    node_ids: Mapping[str, str],
    completion_order: Sequence[str] = (),
    annotations: Sequence[KnownTestFailureAnnotation] = (),
    revalidate: frozenset[str] = frozenset(),
    last_report: KnownTestFailureReport | None = None,
) -> None:
    _atomic_write_json(
        state_path,
        {
            "v": PIN_CHECKPOINT_VERSION,
            "s": suite,
            "pins": {
                uri: {"ok": list(marker.completed_steps)}
                for uri, marker in sorted(markers.items())
            },
            "nodes": dict(node_ids),
            "completion_order": list(completion_order),
            "known_failures": [
                item.to_dict()
                for item in sorted(annotations, key=lambda item: item.identity.uri)
            ],
            "revalidate": sorted(revalidate),
            "last_report": None if last_report is None else last_report.to_dict(),
        },
    )


def _try_framework_pins(root: Path) -> tuple[Pin, Pin] | None:
    try:
        from literate_ai.adapters.project_lifecycle_driver import (
            ProjectLifecycleDriverAdapterError,
            lifecycle_driver_implementation_identity,
        )
        from literate_ai.application.project_authority import (
            AUTHORITY_REVIEW_MARKER,
            AUTHORITY_REVIEW_PLACEHOLDER,
            documentation_path_is_execution_queue,
        )
        from literate_ai.projects import (
            ProjectError,
            discover_project,
            documentation_files,
        )
    except ImportError:
        return None
    try:
        project = discover_project(root)
        if project is None or project.definition.lifecycle_driver is None:
            return None
        tcb_identity = lifecycle_driver_implementation_identity(
            project, project.definition.lifecycle_driver
        )
        placeholder = AUTHORITY_REVIEW_PLACEHOLDER.encode("utf-8")
        documents: list[dict[str, str]] = []
        for path in documentation_files(project):
            relative = path.relative_to(project.root).as_posix()
            if documentation_path_is_execution_queue(relative):
                continue
            content = path.read_bytes()
            digest = hashlib.sha256(
                AUTHORITY_REVIEW_MARKER.sub(b"", content).replace(placeholder, b"")
            ).hexdigest()
            documents.append({"path": relative, "identity": "sha256:" + digest})
        docs_identity = canonical_identity(
            {
                "schema": "literate-ai/documentation-authority-pin@1",
                "project_definition": project.definition.identity.uri,
                "documentation": documents,
            }
        )
    except (
        ProjectError,
        ProjectLifecycleDriverAdapterError,
        OSError,
        TypeError,
        ValueError,
        UnicodeError,
    ):
        return None
    return (
        pin_from_framework_tcb(tcb_identity, declared_inputs=("lifecycle-driver-tcb",)),
        pin_from_documentation_authority(
            docs_identity, declared_inputs=("documentation-authority",)
        ),
    )


def _resolve_gate_pins(root: Path, suite: str, gates: Sequence[str]) -> dict[str, Pin]:
    framework = _try_framework_pins(root)
    if framework is None:
        pin = suite_fallback_pin(suite, gates)
        return {gate: pin for gate in gates}
    return bind_gates_to_pins(gates, tcb=framework[0], docs=framework[1])


def _unittest_source_members(
    root: Path,
    tests: Sequence[unittest.TestCase],
    test_keys: Sequence[str],
) -> tuple[tuple[dict[str, str], ...], bool]:
    members: list[dict[str, str]] = []
    complete = True
    for test, test_key in zip(tests, test_keys, strict=True):
        test_class = type(test)
        module = test_class.__module__
        qualifier = test_class.__qualname__
        source_path = inspect.getsourcefile(test_class)
        source: bytes | None = None
        source_label = f"{module}.{qualifier}"
        if source_path is not None:
            path = Path(source_path)
            try:
                source = path.read_bytes()
                try:
                    source_label = path.resolve().relative_to(root.resolve()).as_posix()
                except (OSError, ValueError):
                    source_label = f"module:{module}"
            except OSError:
                source = None
        if source is None:
            try:
                source = inspect.getsource(test_class).encode("utf-8")
            except (OSError, TypeError, UnicodeError):
                complete = False
                source = b""
        members.append(
            {
                "test_key": test_key,
                "source": source_label,
                "sha256": hashlib.sha256(source).hexdigest(),
            }
        )
    return tuple(members), complete


def _resolve_unittest_pin(
    root: Path,
    steps: Sequence[str],
    tests: Sequence[unittest.TestCase],
) -> tuple[Pin, bool]:
    framework = _try_framework_pins(root)
    if framework is None:
        base = suite_fallback_pin("python-unittest", steps)
    else:
        base = replace(
            framework[0],
            pipeline=tuple(steps),
            declared_inputs=("python-unittest",),
        )
    members, complete = _unittest_source_members(root, tests, steps)
    identity = canonical_identity(
        {
            "schema": "literate-ai/python-unittest-source-pin@1",
            "authority": base.identity.uri,
            "tests": members,
            "complete": complete,
        }
    )
    return Pin(
        identity=identity,
        kind=base.kind,
        coordinate=base.coordinate,
        pipeline=tuple(steps),
        declared_inputs=(*base.declared_inputs, "python-test-sources"),
        own_identity=base.identity,
    ), complete


def _optional_receipts(root: Path) -> MemoryTestedTagStore:
    try:
        from literate_ai.contracts.testing import (
            PROJECT_TEST_RECEIPT_FINALIZED_CANDIDATE_SCHEMA,
            PROJECT_TEST_RECEIPT_SCHEMA,
            ProjectTestReceiptFinalizedCandidate,
        )
        from literate_ai.projects import discover_project
    except ImportError:
        return MemoryTestedTagStore()
    try:
        project = discover_project(root)
        relative = None if project is None else project.definition.test_receipt
        if project is None or relative is None:
            return MemoryTestedTagStore()
        target = project.root.joinpath(*Path(relative).parts)
        if target.is_symlink() or not target.is_file():
            return MemoryTestedTagStore()
        value = json.loads(target.read_text(encoding="utf-8"))
        schema = value.get("schema") if isinstance(value, dict) else None
        if schema == PROJECT_TEST_RECEIPT_FINALIZED_CANDIDATE_SCHEMA:
            receipt = ProjectTestReceiptFinalizedCandidate.from_dict(value).receipt
        elif schema == PROJECT_TEST_RECEIPT_SCHEMA:
            receipt = ProjectTestReceipt.from_dict(value)
        else:
            return MemoryTestedTagStore()
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        return MemoryTestedTagStore()
    if not isinstance(receipt, ProjectTestReceipt):
        return MemoryTestedTagStore()
    return MemoryTestedTagStore((receipt,))


def plan_identity(suite: str, tests: Sequence[str]) -> str:
    encoded = json.dumps([suite, *tests], separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_completed(state_path: Path, suite: str, tests: Sequence[str]) -> list[str]:
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(value, dict) or value.get("v") != SCHEMA_VERSION:
        return []
    if value.get("s") != suite or value.get("p") != plan_identity(suite, tests):
        return []
    completed = value.get("ok")
    if not isinstance(completed, list) or any(
        not isinstance(item, str) for item in completed
    ):
        return []
    prefix = list(tests[: len(completed)])
    return completed if completed == prefix else []


def store_completed(
    state_path: Path,
    suite: str,
    tests: Sequence[str],
    completed: Sequence[str],
) -> None:
    _atomic_write_json(
        state_path,
        {
            "v": SCHEMA_VERSION,
            "s": suite,
            "p": plan_identity(suite, tests),
            "ok": list(completed),
        },
    )


def _load_node_ids(
    state_path: Path, suite: str, tests: Sequence[str]
) -> dict[str, str]:
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(value, dict)
        or value.get("v") != SCHEMA_VERSION
        or value.get("s") != suite
        or value.get("p") != plan_identity(suite, tests)
    ):
        return {}
    node_ids = value.get("nodes", {})
    if not isinstance(node_ids, dict):
        return {}
    return {
        key: node_id
        for key, node_id in node_ids.items()
        if isinstance(key, str) and isinstance(node_id, str)
    }


def _store_completed_with_nodes(
    state_path: Path,
    suite: str,
    tests: Sequence[str],
    completed: Sequence[str],
    node_ids: dict[str, str],
) -> None:
    _atomic_write_json(
        state_path,
        {
            "v": SCHEMA_VERSION,
            "s": suite,
            "p": plan_identity(suite, tests),
            "ok": list(completed),
            "nodes": dict(node_ids),
        },
    )


def reset_checkpoint(state_path: Path) -> None:
    state_path.unlink(missing_ok=True)


def run_gates(
    *,
    suite: str,
    gates: Sequence[str],
    state_path: Path,
    runner: GateRunner,
    evidence_run: EvidenceRun | None = None,
    evidence_parent: str | None = None,
    project_root: Path | None = None,
    gate_pins: Mapping[str, Pin] | None = None,
    dispatch: PinDispatchService | None = None,
) -> int:
    if not gates or len(set(gates)) != len(gates):
        raise ValueError("gates must be a non-empty sequence of unique names")
    root = project_root or Path.cwd()
    if evidence_run is None:
        evidence_run = attach_run(root)
    catalog = (
        dict(gate_pins)
        if gate_pins is not None
        else _resolve_gate_pins(root, suite, gates)
    )
    missing = [gate for gate in gates if gate not in catalog]
    if missing:
        raise ValueError(f"gate {missing[0]!r} has no pin")
    markers = CheckpointMarkerStore(state_path, suite)
    if not markers.repair_progress_matches_gate_prefix(gates, catalog):
        print("checkpoint: gate plan changed; reset repair progress", flush=True)
        markers.reset_repair_progress()
    resumed_repair = bool(markers.completion_order)
    receipts = dispatch.receipts if dispatch is not None else _optional_receipts(root)
    service = PinDispatchService(markers=markers, receipts=receipts)
    unique_pins: dict[str, Pin] = {}
    for gate in gates:
        pin = catalog[gate]
        unique_pins.setdefault(pin.identity.uri, pin)
    decisions = {
        item.pin.identity.uri: item
        for item in service.evaluate(tuple(unique_pins.values()))
    }
    for gate in gates:
        pin = catalog[gate]
        scheduled = decisions[pin.identity.uri]
        skip_tested = scheduled.action is DispatchAction.SKIP
        skip_marker = gate in scheduled.completed_steps
        if skip_tested or skip_marker:
            reason = (
                "pin tested at current hash"
                if skip_tested
                else "passed earlier in this repair run"
            )
            print(f"checkpoint: skip {gate} ({reason})", flush=True)
            if evidence_run is not None:
                context = evidence_run.node(
                    f"release/gate/{suite}/{gate}",
                    operation="release.gate.checkpoint",
                    parent=evidence_parent,
                    pins={"gate": gate, "pin": pin.identity.uri},
                )
                with context as node:
                    node.skip(reason, evidence_node_id=markers.node_ids.get(gate))
            continue
        print(f"checkpoint: run {gate}", flush=True)
        if evidence_run is None:
            result = runner(gate)
        else:
            context = evidence_run.node(
                f"release/gate/{suite}/{gate}",
                operation="release.gate.checkpoint",
                parent=evidence_parent,
                pins={"gate": gate, "pin": pin.identity.uri},
            )
            with context as node:
                result = _run_gate(runner, gate, node)
        if result != 0:
            reusable = sum(len(item.completed_steps) for item in decisions.values())
            print(
                "checkpoint: stop after "
                f"{gate} failed; {reusable} gate(s) remain reusable",
                file=sys.stderr,
                flush=True,
            )
            return result
        marker = service.record_step(pin, gate)
        decisions[pin.identity.uri] = replace(
            scheduled,
            action=DispatchAction.RESUME,
            completed_steps=marker.completed_steps,
        )
        if evidence_run is not None:
            markers.node_ids[gate] = node.node_id
            markers.save(marker)
    markers.reset_repair_progress()
    if resumed_repair:
        print(
            "checkpoint: repaired gate plan completed; reset and rerun from "
            "gate one for release evidence",
            flush=True,
        )
        return 2
    print("checkpoint: complete; reset to the beginning for the next run", flush=True)
    return 0


def _flatten(suite: unittest.TestSuite) -> Iterable[unittest.TestCase]:
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _flatten(test)
        else:
            yield test


class _CheckpointResult(unittest.TextTestResult):
    def __init__(
        self,
        *args: object,
        passed: list[str],
        test_keys: dict[int, str],
        record: Callable[[str], None],
        record_outcome: Callable[
            [str, TestOutcomeStatus, ContentIdentity | None], None
        ],
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._passed = passed
        self._test_keys = test_keys
        self._record = record
        self._record_outcome = record_outcome

    def _record_known(self, test: unittest.TestCase) -> None:
        key = self._test_keys.get(id(test))
        if key is not None:
            self._record(key)

    def addSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().addSuccess(test)
        self._record_known(test)
        self._record_result(test, TestOutcomeStatus.PASSED)

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:  # noqa: N802
        super().addSkip(test, reason)
        self._record_known(test)
        self._record_result(test, TestOutcomeStatus.SKIPPED)

    def addExpectedFailure(  # noqa: N802
        self,
        test: unittest.TestCase,
        err: tuple[type[BaseException], BaseException, object],
    ) -> None:
        super().addExpectedFailure(test, err)
        self._record_known(test)
        self._record_result(test, TestOutcomeStatus.SKIPPED)

    def addFailure(  # noqa: N802
        self,
        test: unittest.TestCase,
        err: tuple[type[BaseException], BaseException, object],
    ) -> None:
        super().addFailure(test, err)
        self._record_result(test, TestOutcomeStatus.FAILED, _failure_identity(err))

    def addError(  # noqa: N802
        self,
        test: unittest.TestCase,
        err: tuple[type[BaseException], BaseException, object],
    ) -> None:
        super().addError(test, err)
        self._record_result(test, TestOutcomeStatus.FAILED, _failure_identity(err))

    def _record_result(
        self,
        test: unittest.TestCase,
        status: TestOutcomeStatus,
        failure_identity: ContentIdentity | None = None,
    ) -> None:
        key = self._test_keys.get(id(test))
        if key is not None:
            self._record_outcome(key, status, failure_identity)


def _failure_identity(
    error: tuple[type[BaseException], BaseException, object],
) -> ContentIdentity:
    exception_type, exception, _traceback = error
    return canonical_identity(
        {
            "schema": "literate-ai/test-failure-fingerprint@1",
            "exception": f"{exception_type.__module__}.{exception_type.__qualname__}",
            "message": str(exception)[:4096],
        }
    )


def _write_known_failure_report(
    path: Path | None, report: KnownTestFailureReport
) -> None:
    if path is not None:
        _atomic_write_json(path, report.to_dict())


def _load_reusable_tests(
    state_path: Path,
    suite: str,
    tests: Sequence[str],
    fingerprints: dict[str, str],
) -> list[str]:
    """Legacy fingerprint checkpoints are not reusable; pin-hash markers are."""

    del fingerprints
    markers, _nodes, _order, _annotations, _revalidate, _report = _load_pin_checkpoint(
        state_path, suite
    )
    if len(markers) != 1:
        return []
    completed = next(iter(markers.values())).completed_steps
    available = set(tests)
    return [item for item in tests if item in completed and item in available]


def _store_reusable_tests(
    state_path: Path,
    suite: str,
    tests: Sequence[str],
    fingerprints: dict[str, str],
    completed: Sequence[str],
) -> None:
    del tests, fingerprints
    identity = suite_fallback_pin(suite, completed).identity
    _store_pin_checkpoint(
        state_path,
        suite,
        {identity.uri: PinMarker(identity, tuple(completed))},
        {},
        completed,
    )


def run_unittest_suite(
    suite: unittest.TestSuite,
    *,
    state_path: Path,
    stream: TextIO,
    verbosity: int = 1,
    pin: Pin | None = None,
    dispatch: PinDispatchService | None = None,
    project_root: Path | None = None,
    context_identity: ContentIdentity | None = None,
    release_evidence: bool = False,
    report_path: Path | None = None,
    now: datetime | None = None,
) -> int:
    tests = tuple(_flatten(suite))
    test_ids = tuple(test.id() for test in tests)
    if not test_ids:
        raise ValueError("test discovery must return at least one test")
    totals = Counter(test_ids)
    occurrences: Counter[str] = Counter()
    test_keys: list[str] = []
    for test_id in test_ids:
        occurrences[test_id] += 1
        test_keys.append(
            test_id if totals[test_id] == 1 else f"{test_id}#{occurrences[test_id]}"
        )
    test_key_by_object = {
        id(test): key for test, key in zip(tests, test_keys, strict=True)
    }
    root = project_root or Path.cwd()
    if pin is None:
        bound, source_complete = _resolve_unittest_pin(root, test_keys, tests)
    else:
        bound, source_complete = pin, True
    if bound.pipeline != tuple(test_keys):
        bound = replace(bound, pipeline=tuple(test_keys))
    markers = CheckpointMarkerStore(state_path, "python-unittest")
    receipts = dispatch.receipts if dispatch is not None else _optional_receipts(root)
    service = PinDispatchService(markers=markers, receipts=receipts)
    scheduled = service.evaluate((bound,))[0]
    if scheduled.action is DispatchAction.SKIP and not release_evidence:
        print(
            f"checkpoint: skip {len(test_keys)} Python test(s) (pin tested at "
            f"{bound.identity.uri})",
            file=stream,
        )
        return 0
    passed = (
        []
        if release_evidence
        else [key for key in test_keys if key in scheduled.completed_steps]
    )
    passed_set = set(passed)
    annotations = {
        key: annotation
        for key in test_keys
        if source_complete
        and not release_evidence
        and (
            annotation := markers.matching_annotation(
                bound.identity, key, context_identity, now=now
            )
        )
        is not None
    }
    remaining = unittest.TestSuite(
        test
        for test, key in zip(tests, test_keys, strict=True)
        if key not in passed_set and key not in annotations
    )
    if passed:
        print(
            f"checkpoint: skip {len(passed)} previously passing Python test(s)",
            file=stream,
        )
    for key, annotation in annotations.items():
        print(
            "checkpoint: known-failure "
            f"{key} ({annotation.cause.value}; {annotation.reason})",
            file=stream,
        )

    completed_set = set(passed)
    outcomes = {
        key: KnownTestFailureOutcome(key, TestOutcomeStatus.PASSED) for key in passed
    }
    outcomes.update(
        {
            key: KnownTestFailureOutcome(
                key,
                TestOutcomeStatus.KNOWN_FAILURE,
                failure_identity=annotation.failure_identity,
                annotation_identity=annotation.identity,
            )
            for key, annotation in annotations.items()
        }
    )

    def record(test_key: str) -> None:
        completed_set.add(test_key)
        service.record_step(bound, test_key)

    def record_outcome(
        test_key: str,
        status: TestOutcomeStatus,
        failure_identity: ContentIdentity | None,
    ) -> None:
        outcomes[test_key] = KnownTestFailureOutcome(
            test_key, status, failure_identity=failure_identity
        )

    class Result(_CheckpointResult):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(
                *args,
                passed=passed,
                test_keys=test_key_by_object,
                record=record,
                record_outcome=record_outcome,
                **kwargs,
            )

    result = unittest.TextTestRunner(
        stream=stream,
        verbosity=verbosity,
        failfast=True,
        resultclass=Result,
    ).run(remaining)
    for key, outcome in tuple(outcomes.items()):
        if outcome.status is not TestOutcomeStatus.PASSED:
            continue
        markers.clear_successful_annotation(
            bound.identity,
            key,
            context_identity,
            release_evidence=release_evidence,
            now=now,
        )
    ordered_outcomes = tuple(outcomes[key] for key in test_keys if key in outcomes)
    counts = Counter(item.status for item in ordered_outcomes)
    report = KnownTestFailureReport(
        suite="python-unittest",
        pin_identity=bound.identity,
        context_identity=context_identity,
        release_evidence=release_evidence,
        summary=KnownTestFailureSummary(
            selected=len(test_keys),
            executed=(
                counts[TestOutcomeStatus.PASSED] + counts[TestOutcomeStatus.FAILED]
            ),
            passed=counts[TestOutcomeStatus.PASSED],
            failed=counts[TestOutcomeStatus.FAILED],
            known_failed=counts[TestOutcomeStatus.KNOWN_FAILURE],
            skipped=counts[TestOutcomeStatus.SKIPPED],
        ),
        outcomes=ordered_outcomes,
    )
    _write_known_failure_report(report_path, report)
    markers.record_report(report)
    if not result.wasSuccessful():
        return 1
    if annotations:
        print(
            "checkpoint: repair remains non-passing with "
            f"{len(annotations)} known failure(s)",
            file=stream,
        )
        return 1
    markers.reset_repair_progress()
    if passed_set:
        print(
            "checkpoint: repaired Python suite completed; reset and rerun from "
            "test one for release evidence",
            file=stream,
        )
        return 2
    return 0


def discover_and_run_unittests(
    *,
    state_path: Path,
    start_directory: str = "tests",
    pattern: str = "test*.py",
    top_level_directory: Path | None = None,
    stream: TextIO = sys.stderr,
    verbosity: int = 1,
    pin: Pin | None = None,
    dispatch: PinDispatchService | None = None,
    context_identity: ContentIdentity | None = None,
    release_evidence: bool = False,
    report_path: Path | None = None,
) -> int:
    root = top_level_directory or Path.cwd()
    suite = unittest.defaultTestLoader.discover(
        start_directory,
        pattern=pattern,
        top_level_dir=root,
    )
    return run_unittest_suite(
        suite,
        state_path=state_path,
        stream=stream,
        verbosity=verbosity,
        pin=pin,
        dispatch=dispatch,
        project_root=root,
        context_identity=context_identity,
        release_evidence=release_evidence,
        report_path=report_path,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    python = commands.add_parser("python", help="run checkpointed unittest discovery")
    python.add_argument("--state", type=Path, required=True)
    python.add_argument("--start-directory", default="tests")
    python.add_argument("--pattern", default="test*.py")
    python.add_argument("--verbosity", type=int, default=1)
    python.add_argument("--context-identity")
    python.add_argument("--report", type=Path)
    python.add_argument(
        "--release-evidence",
        action="store_true",
        help="run from test one and do not reuse known-failure annotations",
    )
    gates = commands.add_parser("gates", help="run checkpointed Make targets")
    gates.add_argument("--state", type=Path, required=True)
    gates.add_argument("--suite", default="release-check")
    gates.add_argument("--make", default=os.environ.get("MAKE", "make"))
    gates.add_argument("--reset", action="store_true")
    gates.add_argument("gates", nargs="*")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    state_path = arguments.state.resolve()
    if arguments.command == "python":
        try:
            context_identity = (
                None
                if arguments.context_identity is None
                else ContentIdentity.parse_uri(arguments.context_identity)
            )
        except ValueError as exc:
            raise SystemExit("--context-identity must be a sha256 identity") from exc
        return discover_and_run_unittests(
            state_path=state_path,
            start_directory=arguments.start_directory,
            pattern=arguments.pattern,
            verbosity=arguments.verbosity,
            context_identity=context_identity,
            release_evidence=arguments.release_evidence,
            report_path=arguments.report,
        )
    if arguments.reset:
        reset_checkpoint(state_path)
        print(f"checkpoint: reset {state_path}")
        return 0
    if not arguments.gates:
        raise SystemExit("at least one gate is required unless --reset is used")

    project_root = Path.cwd()
    evidence_run = attach_run(project_root)
    owns_evidence_run = evidence_run is None
    if owns_evidence_run:
        evidence_run = open_run(project_root, operation="release.gates")
    if evidence_run is not None:
        report_progress(f"Release evidence root: {evidence_run.root}")

    def run_make(gate: str, *, evidence_node: EvidenceNode | None = None) -> int:
        if evidence_run is None:
            return subprocess.run(
                [arguments.make, "--no-print-directory", gate], check=False
            ).returncode
        completed = record_subprocess(
            [arguments.make, "--no-print-directory", gate],
            cwd=project_root,
            run=evidence_run,
            parent=evidence_node.node_id if evidence_node is not None else None,
            path=f"release/gate/{arguments.suite}/{gate}",
            operation="release.gate",
            node=evidence_node,
            tee=True,
        )
        return completed.returncode

    evidence_context = (
        evidence_run.node("release", operation="release.gates")
        if evidence_run is not None
        else None
    )
    state = "failed"
    result = 1
    try:
        if evidence_context is None:
            result = run_gates(
                suite=arguments.suite,
                gates=arguments.gates,
                state_path=state_path,
                runner=run_make,
                project_root=project_root,
            )
        else:
            with evidence_context as node:
                evidence_node = node if isinstance(node, EvidenceNode) else None
                result = run_gates(
                    suite=arguments.suite,
                    gates=arguments.gates,
                    state_path=state_path,
                    runner=run_make,
                    evidence_run=evidence_run,
                    evidence_parent=(
                        evidence_node.node_id if evidence_node is not None else None
                    ),
                    project_root=project_root,
                )
                if result != 0 and isinstance(evidence_node, EvidenceNode):
                    evidence_node.fail(
                        f"subprocess.exit: release gate exited with status {result}"
                    )
        state = "passed" if result == 0 else "failed"
        return result
    finally:
        if evidence_run is not None and owns_evidence_run:
            evidence_run.close(state)


if __name__ == "__main__":
    raise SystemExit(main())
