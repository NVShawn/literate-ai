"""CLI adapter for explicit known-failure checkpoint operations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.known_test_failures import (
    KnownTestFailureAnnotation,
    KnownTestFailureCause,
    KnownTestFailurePolicy,
)
from literate_ai.known_failure_checkpoints import (
    KnownFailureCheckpointError,
    annotate_known_failure,
    clear_known_failure,
    import_external_test_events,
    inspect_known_failures,
    request_known_failure_revalidation,
)
from literate_ai.projects import ProjectConfigurationStore

from .errors import CliFailure


def _identity(value: str, label: str) -> ContentIdentity:
    try:
        return ContentIdentity.parse_uri(value)
    except (TypeError, ValueError) as exc:
        raise CliFailure(
            "known_failure.identity_invalid", f"{label} must be a sha256 identity"
        ) from exc


def _state(args: Any) -> Path:
    project = ProjectConfigurationStore.discover(Path(args.project))
    if project is None:
        raise CliFailure("project.not_found", "no Literate AI project was found")
    requested = Path(args.state)
    return (
        requested if requested.is_absolute() else project.root / requested
    ).resolve()


def test_checkpoint_from_args(args: Any) -> tuple[dict[str, object], int]:
    state = _state(args)
    suite = args.suite
    operation = args.test_checkpoint_command
    try:
        if operation == "inspect":
            annotations = inspect_known_failures(state, suite)
            return {
                "schema": "literate-ai/known-failure-checkpoint-operation@1",
                "operation": operation,
                "state": str(state),
                "suite": suite,
                "annotations": [item.to_dict() for item in annotations],
            }, 0
        if operation == "annotate":
            annotation = KnownTestFailureAnnotation(
                pin_identity=_identity(args.pin, "--pin"),
                test_key=args.test_key,
                cause=KnownTestFailureCause(args.cause),
                context_identity=(
                    None
                    if args.context_identity is None
                    else _identity(args.context_identity, "--context-identity")
                ),
                universal_authority=(
                    None
                    if args.universal_authority is None
                    else _identity(args.universal_authority, "--universal-authority")
                ),
                failure_identity=_identity(args.failure_identity, "--failure-identity"),
                evidence_run_identity=_identity(
                    args.evidence_run_identity, "--evidence-run-identity"
                ),
                evidence_node_id=args.evidence_node_id,
                reason=args.reason,
                policy=(
                    KnownTestFailurePolicy.EXPIRES_AT
                    if args.expires_at is not None
                    else KnownTestFailurePolicy.MANUAL_REVALIDATION
                ),
                expires_at=args.expires_at,
                tracker=args.tracker,
            )
            annotate_known_failure(state, suite, annotation)
            return {
                "schema": "literate-ai/known-failure-checkpoint-operation@1",
                "operation": operation,
                "state": str(state),
                "suite": suite,
                "annotation": annotation.to_dict(),
                "annotation_identity": annotation.identity.uri,
            }, 0
        if operation == "clear":
            pin = _identity(args.pin, "--pin")
            changed = clear_known_failure(state, suite, pin, args.test_key)
            return {
                "schema": "literate-ai/known-failure-checkpoint-operation@1",
                "operation": operation,
                "state": str(state),
                "suite": suite,
                "changed": changed,
            }, 0
        if operation == "revalidate":
            pin = _identity(args.pin, "--pin")
            annotation = request_known_failure_revalidation(
                state, suite, pin, args.test_key
            )
            return {
                "schema": "literate-ai/known-failure-checkpoint-operation@1",
                "operation": operation,
                "state": str(state),
                "suite": suite,
                "annotation_identity": annotation.identity.uri,
                "next_run": "execute",
            }, 0
        try:
            document = json.loads(Path(args.report).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CliFailure(
                "known_failure.external_report_invalid",
                "external report is not readable canonical JSON",
            ) from exc
        report = import_external_test_events(
            document, state_path=state, release_evidence=args.release_evidence
        )
        return report.to_dict(), 0 if report.passing else 1
    except (TypeError, ValueError, KnownFailureCheckpointError) as exc:
        raise CliFailure(
            getattr(exc, "code", "known_failure.annotation_invalid"), str(exc)
        ) from exc


__all__ = ["test_checkpoint_from_args"]
