"""Known failures remain visible debt while exact repair runs avoid repetition."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.application.pinned_test_dispatch import Pin, PinKind
from literate_ai.cli import main
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.known_test_failures import (
    KnownTestFailureAnnotation,
    KnownTestFailureCause,
    KnownTestFailurePolicy,
    KnownTestFailureReport,
)
from literate_ai.known_failure_checkpoints import (
    annotate_known_failure,
    inspect_known_failures,
)
from literate_ai.test_checkpointing import run_unittest_suite

ROOT = Path(__file__).resolve().parents[2]


def identity(label: str) -> ContentIdentity:
    return canonical_identity({"fixture": label})


def annotation(
    pin: ContentIdentity,
    test_key: str,
    *,
    context: ContentIdentity | None = None,
    universal: bool = False,
    failure: ContentIdentity | None = None,
    expires_at: str | None = None,
    reason: str = "External content requires interactive authentication",
) -> KnownTestFailureAnnotation:
    return KnownTestFailureAnnotation(
        pin_identity=pin,
        test_key=test_key,
        cause=KnownTestFailureCause.AUTHENTICATION_REQUIRED,
        context_identity=None if universal else (context or identity("worker")),
        universal_authority=identity("operator-authority") if universal else None,
        failure_identity=failure or identity("original-failure"),
        evidence_run_identity=identity("evidence-run"),
        evidence_node_id="n0007",
        reason=reason,
        policy=(
            KnownTestFailurePolicy.EXPIRES_AT
            if expires_at is not None
            else KnownTestFailurePolicy.MANUAL_REVALIDATION
        ),
        expires_at=expires_at,
        tracker="https://example.test/issues/7",
    )


def bound_pin(keys: tuple[str, ...]) -> Pin:
    return Pin(
        identity=identity("source-pin"),
        kind=PinKind.FRAMEWORK_TCB,
        coordinate="known-failure-fixture",
        pipeline=keys,
        declared_inputs=("fixture",),
    )


class KnownFailurePythonRunnerTests(unittest.TestCase):
    def test_matching_annotation_suppresses_only_exact_test_and_remains_nonpassing(
        self,
    ) -> None:
        executions: list[str] = []

        class Cases(unittest.TestCase):
            def test_a(self) -> None:
                executions.append("a")
                self.fail("interactive authentication required")

            def test_b(self) -> None:
                executions.append("b")

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Cases)
        keys = tuple(test.id() for test in suite)
        pin = bound_pin(keys)
        context = identity("worker")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "checkpoint.json"
            report_path = root / "report.json"
            annotate_known_failure(
                state, "python-unittest", annotation(pin.identity, keys[0])
            )
            result = run_unittest_suite(
                unittest.defaultTestLoader.loadTestsFromTestCase(Cases),
                state_path=state,
                stream=io.StringIO(),
                pin=pin,
                context_identity=context,
                report_path=report_path,
            )
            report = KnownTestFailureReport.from_dict(
                json.loads(report_path.read_text(encoding="utf-8"))
            )
            checkpoint = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(result, 1)
        self.assertEqual(executions, ["b"])
        self.assertEqual(report.summary.known_failed, 1)
        self.assertEqual(report.summary.passed, 1)
        self.assertFalse(report.passing)
        self.assertEqual(checkpoint["last_report"], report.to_dict())

    def test_changed_context_expiry_and_release_run_execute_normally(self) -> None:
        executions: list[str] = []

        class Cases(unittest.TestCase):
            def test_case(self) -> None:
                executions.append("case")

        key = next(iter(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))).id()
        pin = bound_pin((key,))
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "checkpoint.json"
            annotate_known_failure(
                state,
                "python-unittest",
                annotation(pin.identity, key, context=identity("worker-a")),
            )
            self.assertEqual(
                run_unittest_suite(
                    unittest.defaultTestLoader.loadTestsFromTestCase(Cases),
                    state_path=state,
                    stream=io.StringIO(),
                    pin=pin,
                    context_identity=identity("worker-b"),
                ),
                0,
            )
            self.assertEqual(executions, ["case"])

            expired = (
                (datetime.now(UTC) - timedelta(seconds=1))
                .isoformat()
                .replace("+00:00", "Z")
            )
            annotate_known_failure(
                state,
                "python-unittest",
                annotation(pin.identity, key, expires_at=expired),
            )
            executions.clear()
            self.assertEqual(
                run_unittest_suite(
                    unittest.defaultTestLoader.loadTestsFromTestCase(Cases),
                    state_path=state,
                    stream=io.StringIO(),
                    pin=pin,
                    context_identity=identity("worker"),
                ),
                0,
            )
            self.assertEqual(executions, ["case"])

            annotate_known_failure(
                state, "python-unittest", annotation(pin.identity, key)
            )
            executions.clear()
            self.assertEqual(
                run_unittest_suite(
                    unittest.defaultTestLoader.loadTestsFromTestCase(Cases),
                    state_path=state,
                    stream=io.StringIO(),
                    pin=pin,
                    context_identity=identity("worker"),
                    release_evidence=True,
                ),
                0,
            )
            self.assertEqual(executions, ["case"])
            self.assertEqual(inspect_known_failures(state, "python-unittest"), ())


class KnownFailureCliTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> tuple[int, dict[str, object]]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        status = main(arguments, stdout=stdout, stderr=stderr)
        content = stdout.getvalue() if status == 0 else stderr.getvalue()
        return status, json.loads(content)

    def test_annotate_inspect_revalidate_and_clear_are_explicit(self) -> None:
        pin = identity("cli-pin")
        key = "fixture.Cases.test_authentication"
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "checkpoint.json"
            status, annotated = self.invoke(
                "project",
                "test-checkpoint",
                "annotate",
                "--project",
                str(ROOT),
                "--state",
                str(state),
                "--pin",
                pin.uri,
                "--test-key",
                key,
                "--cause",
                KnownTestFailureCause.AUTHENTICATION_REQUIRED.value,
                "--context-identity",
                identity("cli-worker").uri,
                "--failure-identity",
                identity("cli-failure").uri,
                "--evidence-run-identity",
                identity("cli-evidence").uri,
                "--evidence-node-id",
                "n0007",
                "--reason",
                "Authentication fixture requires operator credentials",
            )
            self.assertEqual(status, 0, annotated)
            status, inspected = self.invoke(
                "project",
                "test-checkpoint",
                "inspect",
                "--project",
                str(ROOT),
                "--state",
                str(state),
            )
            self.assertEqual(status, 0, inspected)
            self.assertEqual(len(inspected["result"]["annotations"]), 1)
            status, revalidated = self.invoke(
                "project",
                "test-checkpoint",
                "revalidate",
                "--project",
                str(ROOT),
                "--state",
                str(state),
                "--pin",
                pin.uri,
                "--test-key",
                key,
            )
            self.assertEqual(status, 0, revalidated)
            self.assertEqual(revalidated["result"]["next_run"], "execute")
            status, cleared = self.invoke(
                "project",
                "test-checkpoint",
                "clear",
                "--project",
                str(ROOT),
                "--state",
                str(state),
                "--pin",
                pin.uri,
                "--test-key",
                key,
            )
            self.assertEqual(status, 0, cleared)
            self.assertTrue(cleared["result"]["changed"])


if __name__ == "__main__":
    unittest.main()
