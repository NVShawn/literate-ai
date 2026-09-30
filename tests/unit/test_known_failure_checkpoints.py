"""Known failures remain visible debt while exact repair runs avoid repetition."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from literate_ai.application.pinned_test_dispatch import Pin, PinKind
from literate_ai.cli import main
from literate_ai.contracts import ContractValidationError
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.known_test_failures import (
    KnownTestFailureAnnotation,
    KnownTestFailureCause,
    KnownTestFailurePolicy,
    KnownTestFailureReport,
)
from literate_ai.known_failure_checkpoints import (
    EXTERNAL_TEST_EVENTS_SCHEMA,
    annotate_known_failure,
    clear_known_failure,
    import_external_test_events,
    inspect_known_failures,
    request_known_failure_revalidation,
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


class KnownFailureContractTests(unittest.TestCase):
    def test_annotation_round_trips_and_universal_authority_is_explicit(self) -> None:
        value = annotation(identity("pin"), "fixture.Cases.test_case", universal=True)
        self.assertEqual(KnownTestFailureAnnotation.from_dict(value.to_dict()), value)
        self.assertTrue(value.is_universal)

    def test_annotation_rejects_secret_shaped_reason_and_ambiguous_context(
        self,
    ) -> None:
        with self.assertRaises(ContractValidationError):
            annotation(
                identity("pin"),
                "fixture.Cases.test_case",
                reason="Bearer abcdefghijklmnopqrstuvwxyz",
            )
        value = annotation(identity("pin"), "fixture.Cases.test_case")
        payload = value.to_dict()
        payload["universal_authority"] = identity("authority").to_dict()
        with self.assertRaises(ContractValidationError):
            KnownTestFailureAnnotation.from_dict(payload)


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

    def test_changed_source_pin_executes_normally(self) -> None:
        executions: list[str] = []

        class Cases(unittest.TestCase):
            def test_case(self) -> None:
                executions.append("case")

        key = next(iter(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))).id()
        original = bound_pin((key,))
        changed = Pin(
            identity=identity("changed-source-pin"),
            kind=original.kind,
            coordinate=original.coordinate,
            pipeline=original.pipeline,
            declared_inputs=original.declared_inputs,
        )
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "checkpoint.json"
            annotate_known_failure(
                state, "python-unittest", annotation(original.identity, key)
            )
            self.assertEqual(
                run_unittest_suite(
                    unittest.defaultTestLoader.loadTestsFromTestCase(Cases),
                    state_path=state,
                    stream=io.StringIO(),
                    pin=changed,
                    context_identity=identity("worker"),
                ),
                0,
            )
        self.assertEqual(executions, ["case"])

    def test_default_pin_folds_test_source_bytes(self) -> None:
        executions: list[str] = []

        class Cases(unittest.TestCase):
            def test_case(self) -> None:
                executions.append("case")

        key = next(iter(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))).id()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "checkpoint.json"
            report_path = root / "report.json"
            source = root / "test_fixture.py"
            source.write_text("revision = 1\n", encoding="utf-8")
            with mock.patch(
                "literate_ai.test_checkpointing.inspect.getsourcefile",
                return_value=str(source),
            ):
                self.assertEqual(
                    run_unittest_suite(
                        unittest.defaultTestLoader.loadTestsFromTestCase(Cases),
                        state_path=state,
                        stream=io.StringIO(),
                        project_root=root,
                        context_identity=identity("worker"),
                        report_path=report_path,
                    ),
                    0,
                )
            original_report = KnownTestFailureReport.from_dict(
                json.loads(report_path.read_text(encoding="utf-8"))
            )
            annotate_known_failure(
                state,
                "python-unittest",
                annotation(original_report.pin_identity, key),
            )
            source.write_text("revision = 2\n", encoding="utf-8")
            executions.clear()
            with mock.patch(
                "literate_ai.test_checkpointing.inspect.getsourcefile",
                return_value=str(source),
            ):
                self.assertEqual(
                    run_unittest_suite(
                        unittest.defaultTestLoader.loadTestsFromTestCase(Cases),
                        state_path=state,
                        stream=io.StringIO(),
                        project_root=root,
                        context_identity=identity("worker"),
                    ),
                    0,
                )
        self.assertEqual(executions, ["case"])

    def test_context_mismatch_pass_preserves_other_workers_annotation(self) -> None:
        class Cases(unittest.TestCase):
            def test_case(self) -> None:
                return None

        key = next(iter(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))).id()
        pin = bound_pin((key,))
        retained = annotation(pin.identity, key, context=identity("worker-a"))
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "checkpoint.json"
            annotate_known_failure(state, "python-unittest", retained)
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
            checkpoint = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["pins"], {})
            self.assertEqual(
                checkpoint["known_failures"],
                [retained.to_dict()],
            )
            self.assertEqual(checkpoint["last_report"]["summary"]["passed"], 1)

    def test_explicit_revalidation_pass_clears_annotation(self) -> None:
        class Cases(unittest.TestCase):
            def test_case(self) -> None:
                pass

        key = next(iter(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))).id()
        pin = bound_pin((key,))
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "checkpoint.json"
            annotate_known_failure(
                state, "python-unittest", annotation(pin.identity, key)
            )
            request_known_failure_revalidation(
                state, "python-unittest", pin.identity, key
            )
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
            self.assertEqual(inspect_known_failures(state, "python-unittest"), ())

    def test_malformed_checkpoint_suppresses_nothing(self) -> None:
        executions: list[str] = []

        class Cases(unittest.TestCase):
            def test_case(self) -> None:
                executions.append("case")

        key = next(iter(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))).id()
        pin = bound_pin((key,))
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "checkpoint.json"
            state.write_text(
                json.dumps(
                    {
                        "v": 4,
                        "s": "python-unittest",
                        "pins": {},
                        "nodes": {},
                        "known_failures": [{"secret": "sk-abcdefghijklmnop"}],
                        "revalidate": [],
                    }
                ),
                encoding="utf-8",
            )
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


class ExternalResultImportTests(unittest.TestCase):
    def test_unmatched_start_fails_closed_then_exact_annotation_is_imported(
        self,
    ) -> None:
        pin = identity("external-pin")
        context = identity("worker")
        key = "extension.fixture.authentication_test"
        first = {
            "schema": EXTERNAL_TEST_EVENTS_SCHEMA,
            "suite": "retained-extension-tests",
            "pin_identity": pin.to_dict(),
            "context_identity": context.to_dict(),
            "selected_tests": [key, "extension.fixture.later_test"],
            "events": [
                {
                    "test_key": key,
                    "event": "start",
                    "failure_identity": None,
                    "annotation_identity": None,
                },
                {
                    "test_key": "extension.fixture.later_test",
                    "event": "start",
                    "failure_identity": None,
                    "annotation_identity": None,
                },
                {
                    "test_key": "extension.fixture.later_test",
                    "event": "passed",
                    "failure_identity": None,
                    "annotation_identity": None,
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "checkpoint.json"
            original = import_external_test_events(first, state_path=state)
            self.assertEqual(original.summary.failed, 1)
            self.assertEqual(original.summary.passed, 1)
            failure = next(
                item.failure_identity
                for item in original.outcomes
                if item.test_key == key
            )
            self.assertIsNotNone(failure)
            retained = annotation(pin, key, context=context, failure=failure)
            annotate_known_failure(state, "retained-extension-tests", retained)
            second = dict(first)
            second["events"] = [
                {
                    "test_key": key,
                    "event": "known-failure",
                    "failure_identity": failure.to_dict(),
                    "annotation_identity": retained.identity.to_dict(),
                },
                *first["events"][1:],
            ]
            report = import_external_test_events(second, state_path=state)
            checkpoint = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(report.summary.known_failed, 1)
            self.assertEqual(report.summary.passed, 1)
            self.assertFalse(report.passing)
            self.assertEqual(checkpoint["last_report"], report.to_dict())
            self.assertTrue(clear_known_failure(state, report.suite, pin, key))

    def test_changed_fingerprint_and_release_reject_known_failure(self) -> None:
        pin = identity("external-pin")
        context = identity("worker")
        key = "extension.fixture.authentication_test"
        retained = annotation(pin, key, context=context)
        event = {
            "schema": EXTERNAL_TEST_EVENTS_SCHEMA,
            "suite": "retained-extension-tests",
            "pin_identity": pin.to_dict(),
            "context_identity": context.to_dict(),
            "selected_tests": [key],
            "events": [
                {
                    "test_key": key,
                    "event": "known-failure",
                    "failure_identity": identity("changed-failure").to_dict(),
                    "annotation_identity": retained.identity.to_dict(),
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "checkpoint.json"
            annotate_known_failure(state, "retained-extension-tests", retained)
            with self.assertRaisesRegex(ValueError, "exact reusable annotation"):
                import_external_test_events(event, state_path=state)
            event["events"][0]["failure_identity"] = retained.failure_identity.to_dict()
            with self.assertRaisesRegex(ValueError, "exact reusable annotation"):
                import_external_test_events(
                    event, state_path=state, release_evidence=True
                )

    def test_successful_external_revalidation_clears_exact_annotation(self) -> None:
        pin = identity("external-pin")
        context = identity("worker")
        key = "extension.fixture.authentication_test"
        retained = annotation(pin, key, context=context)
        event = {
            "schema": EXTERNAL_TEST_EVENTS_SCHEMA,
            "suite": "retained-extension-tests",
            "pin_identity": pin.to_dict(),
            "context_identity": context.to_dict(),
            "selected_tests": [key],
            "events": [
                {
                    "test_key": key,
                    "event": "start",
                    "failure_identity": None,
                    "annotation_identity": None,
                },
                {
                    "test_key": key,
                    "event": "passed",
                    "failure_identity": None,
                    "annotation_identity": None,
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "checkpoint.json"
            annotate_known_failure(state, "retained-extension-tests", retained)
            request_known_failure_revalidation(
                state, "retained-extension-tests", pin, key
            )
            report = import_external_test_events(event, state_path=state)
            self.assertTrue(report.passing)
            self.assertEqual(
                inspect_known_failures(state, "retained-extension-tests"), ()
            )


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
