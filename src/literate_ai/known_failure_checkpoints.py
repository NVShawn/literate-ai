"""Known-failure checkpoint API and strict external-runner result import."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.known_test_failures import (
    KnownTestFailureAnnotation,
    KnownTestFailureOutcome,
    KnownTestFailureReport,
    KnownTestFailureSummary,
    TestOutcomeStatus,
)
from literate_ai.test_checkpointing import CheckpointMarkerStore

EXTERNAL_TEST_EVENTS_SCHEMA = "literate-ai/external-test-events@1"
_EVENTS = frozenset({"start", "passed", "failed", "skipped", "known-failure"})


class KnownFailureCheckpointError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def annotate_known_failure(
    state_path: Path, suite: str, annotation: KnownTestFailureAnnotation
) -> KnownTestFailureAnnotation:
    store = CheckpointMarkerStore(state_path, suite)
    store.annotate(annotation)
    return annotation


def inspect_known_failures(
    state_path: Path, suite: str
) -> tuple[KnownTestFailureAnnotation, ...]:
    store = CheckpointMarkerStore(state_path, suite)
    return tuple(sorted(store.annotations.values(), key=lambda item: item.identity.uri))


def clear_known_failure(
    state_path: Path, suite: str, pin_identity: ContentIdentity, test_key: str
) -> bool:
    return CheckpointMarkerStore(state_path, suite).clear(pin_identity, test_key)


def request_known_failure_revalidation(
    state_path: Path, suite: str, pin_identity: ContentIdentity, test_key: str
) -> KnownTestFailureAnnotation:
    annotation = CheckpointMarkerStore(state_path, suite).request_revalidation(
        pin_identity, test_key
    )
    if annotation is None:
        raise KnownFailureCheckpointError(
            "known_failure.not_found", "no exact known-failure annotation was found"
        )
    return annotation


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise KnownFailureCheckpointError(
            "known_failure.external_report_invalid", f"{label} must be an object"
        )
    return value


def _exact_fields(
    value: Any, label: str, required: frozenset[str]
) -> Mapping[str, Any]:
    data = _object(value, label)
    if set(data) != set(required):
        raise KnownFailureCheckpointError(
            "known_failure.external_report_invalid",
            f"{label} must contain exactly: {', '.join(sorted(required))}",
        )
    return data


def _identity(value: Any, label: str) -> ContentIdentity:
    try:
        return ContentIdentity.from_dict(value, path=label)
    except (TypeError, ValueError) as exc:
        raise KnownFailureCheckpointError(
            "known_failure.external_report_invalid",
            f"{label} must be a content identity",
        ) from exc


def _optional_identity(value: Any, label: str) -> ContentIdentity | None:
    return None if value is None else _identity(value, label)


def import_external_test_events(
    document: Any,
    *,
    state_path: Path,
    release_evidence: bool = False,
    now: datetime | None = None,
) -> KnownTestFailureReport:
    """Reconcile per-test events; enclosing process status is intentionally absent."""

    data = _exact_fields(
        document,
        "external report",
        frozenset(
            {
                "schema",
                "suite",
                "pin_identity",
                "context_identity",
                "selected_tests",
                "events",
            }
        ),
    )
    if data["schema"] != EXTERNAL_TEST_EVENTS_SCHEMA:
        raise KnownFailureCheckpointError(
            "known_failure.external_report_invalid", "external report schema is invalid"
        )
    suite = data["suite"]
    if not isinstance(suite, str) or not suite or len(suite) > 256:
        raise KnownFailureCheckpointError(
            "known_failure.external_report_invalid", "suite is invalid"
        )
    pin_identity = _identity(data["pin_identity"], "external report.pin_identity")
    context_identity = _optional_identity(
        data["context_identity"], "external report.context_identity"
    )
    selected_value = data["selected_tests"]
    if not isinstance(selected_value, Sequence) or isinstance(
        selected_value, (str, bytes, bytearray)
    ):
        raise KnownFailureCheckpointError(
            "known_failure.external_report_invalid", "selected_tests must be an array"
        )
    selected = tuple(selected_value)
    if (
        not selected
        or any(not isinstance(item, str) or not item for item in selected)
        or len(set(selected)) != len(selected)
    ):
        raise KnownFailureCheckpointError(
            "known_failure.external_report_invalid",
            "selected_tests must contain unique non-empty exact keys",
        )
    events_value = data["events"]
    if not isinstance(events_value, Sequence) or isinstance(
        events_value, (str, bytes, bytearray)
    ):
        raise KnownFailureCheckpointError(
            "known_failure.external_report_invalid", "events must be an array"
        )
    store = CheckpointMarkerStore(state_path, suite)
    started: set[str] = set()
    terminals: dict[str, KnownTestFailureOutcome] = {}
    selected_set = set(selected)
    current = now or datetime.now(UTC)
    for index, raw_event in enumerate(events_value):
        event = _exact_fields(
            raw_event,
            f"events[{index}]",
            frozenset({"test_key", "event", "failure_identity", "annotation_identity"}),
        )
        key = event["test_key"]
        kind = event["event"]
        if key not in selected_set or not isinstance(key, str):
            raise KnownFailureCheckpointError(
                "known_failure.external_report_invalid",
                f"events[{index}] names an unselected test key",
            )
        if kind not in _EVENTS:
            raise KnownFailureCheckpointError(
                "known_failure.external_report_invalid",
                f"events[{index}] has an invalid event",
            )
        failure = _optional_identity(
            event["failure_identity"], f"events[{index}].failure_identity"
        )
        annotation_identity = _optional_identity(
            event["annotation_identity"], f"events[{index}].annotation_identity"
        )
        if kind == "start":
            if key in started or key in terminals or failure or annotation_identity:
                raise KnownFailureCheckpointError(
                    "known_failure.external_report_invalid",
                    f"events[{index}] is an invalid or duplicate start",
                )
            started.add(key)
            continue
        if key in terminals:
            raise KnownFailureCheckpointError(
                "known_failure.external_report_invalid",
                f"events[{index}] duplicates a terminal result",
            )
        if kind == "known-failure":
            annotation = store.matching_annotation(
                pin_identity, key, context_identity, now=current
            )
            if (
                release_evidence
                or annotation is None
                or key in started
                or failure != annotation.failure_identity
                or annotation_identity != annotation.identity
            ):
                raise KnownFailureCheckpointError(
                    "known_failure.external_report_invalid",
                    f"events[{index}] does not bind an exact reusable annotation",
                )
            terminals[key] = KnownTestFailureOutcome(
                key,
                TestOutcomeStatus.KNOWN_FAILURE,
                failure_identity=failure,
                annotation_identity=annotation_identity,
            )
            continue
        if key not in started or annotation_identity is not None:
            raise KnownFailureCheckpointError(
                "known_failure.external_report_invalid",
                f"events[{index}] terminal has no unique start",
            )
        if kind == "failed":
            if failure is None:
                raise KnownFailureCheckpointError(
                    "known_failure.external_report_invalid",
                    f"events[{index}] failure has no fingerprint",
                )
            terminals[key] = KnownTestFailureOutcome(
                key, TestOutcomeStatus.FAILED, failure_identity=failure
            )
        else:
            if failure is not None:
                raise KnownFailureCheckpointError(
                    "known_failure.external_report_invalid",
                    f"events[{index}] non-failure carries a failure fingerprint",
                )
            terminals[key] = KnownTestFailureOutcome(
                key,
                TestOutcomeStatus.PASSED
                if kind == "passed"
                else TestOutcomeStatus.SKIPPED,
            )
    for key in started - set(terminals):
        terminals[key] = KnownTestFailureOutcome(
            key,
            TestOutcomeStatus.FAILED,
            failure_identity=canonical_identity(
                {
                    "schema": "literate-ai/unmatched-external-test-start@1",
                    "pin": pin_identity.uri,
                    "test_key": key,
                    "context": (
                        None if context_identity is None else context_identity.uri
                    ),
                }
            ),
        )
    outcomes = tuple(terminals[key] for key in selected if key in terminals)
    counts = Counter(item.status for item in outcomes)
    report = KnownTestFailureReport(
        suite=suite,
        pin_identity=pin_identity,
        context_identity=context_identity,
        release_evidence=release_evidence,
        summary=KnownTestFailureSummary(
            selected=len(selected),
            executed=counts[TestOutcomeStatus.PASSED]
            + counts[TestOutcomeStatus.FAILED],
            passed=counts[TestOutcomeStatus.PASSED],
            failed=counts[TestOutcomeStatus.FAILED],
            known_failed=counts[TestOutcomeStatus.KNOWN_FAILURE],
            skipped=counts[TestOutcomeStatus.SKIPPED],
        ),
        outcomes=outcomes,
    )
    for outcome in outcomes:
        if outcome.status is TestOutcomeStatus.PASSED:
            store.clear_successful_annotation(
                pin_identity,
                outcome.test_key,
                context_identity,
                release_evidence=release_evidence,
                now=current,
            )
    store.record_report(report)
    return report


__all__ = [
    "EXTERNAL_TEST_EVENTS_SCHEMA",
    "KnownFailureCheckpointError",
    "annotate_known_failure",
    "clear_known_failure",
    "import_external_test_events",
    "inspect_known_failures",
    "request_known_failure_revalidation",
]
