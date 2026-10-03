"""Exact data custody and real command-process lifecycle-action transport."""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.adapters.action_command_dispatch import (
    CommandLifecycleActionDispatcher,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    decode_action_request,
    decode_action_response,
    encode_action_request,
    encode_action_response,
    record_identity,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDagScheduler,
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
    LifecycleActionNode,
    LifecycleActionWorker,
)
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerEnvironment,
    ExecutionWorkerKind,
)
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes


def request_fixture(worker=None, catalog=None, deadline=None):
    selected = worker or ExecutionWorker(
        "command", ExecutionWorkerKind.COMMAND, command=(sys.executable,)
    )
    catalog = catalog or ExecutionWorkerCatalog((selected,))
    deadline = deadline or ActionDispatchDeadline(
        datetime.now(UTC) + timedelta(minutes=1)
    )
    payload, previous = b"exact phase payload", b"accepted predecessor record"
    records = {record_identity(payload): payload, record_identity(previous): previous}
    action = LifecycleActionNode(
        "component/index",
        canonical_identity("component"),
        LifecycleActionKind.INDEX,
        record_identity(payload),
        ("component/generate",),
        (selected.worker_id,),
    )
    admitted = LifecycleActionWorker(
        selected.worker_id,
        selected.identity,
        catalog.identity,
        canonical_identity("fresh-observation"),
    )
    request = LifecycleActionDispatchRequest(
        canonical_identity("schedule"),
        action,
        admitted,
        0,
        (record_identity(previous),),
        deadline.identity,
    )
    return request, deadline, records


class ActionWireTests(unittest.TestCase):
    def setUp(self):
        self.request, self.deadline, self.records = request_fixture()

    def test_exact_request_and_result_round_trip(self):
        encoded = encode_action_request(self.request, self.deadline, self.records)
        decoded, deadline, records = decode_action_request(encoded)
        self.assertEqual(
            (decoded, deadline, records), (self.request, self.deadline, self.records)
        )
        response = encode_action_response(decoded, result_record=b"verified result")
        outcome, record = decode_action_response(response, self.request)
        self.assertEqual(record, b"verified result")
        self.assertEqual(outcome.result_identity, record_identity(record))
        self.assertEqual(outcome.request_identity, self.request.identity)

    def test_corrupt_missing_extra_and_duplicate_records_refuse(self):
        wire = encode_action_request(self.request, self.deadline, self.records)
        for mode in ("corrupt", "missing", "extra", "duplicate"):
            with self.subTest(mode=mode):
                value = json.loads(wire)
                if mode == "corrupt":
                    value["records"][0]["content"] = base64.b64encode(
                        b"changed"
                    ).decode()
                elif mode == "missing":
                    value["records"].pop()
                elif mode == "extra":
                    value["records"].append(
                        {
                            "identity": record_identity(b"extra").uri,
                            "content": base64.b64encode(b"extra").decode(),
                        }
                    )
                else:
                    value["records"].append(value["records"][0])
                with self.assertRaises(ActionWireError):
                    decode_action_request(canonical_json_bytes(value))

    def test_changed_request_worker_deadline_and_unknown_fields_refuse(self):
        encoded = encode_action_request(self.request, self.deadline, self.records)
        for field in ("schedule_identity", "worker", "deadline", "unknown"):
            with self.subTest(field=field):
                value = json.loads(encoded)
                if field == "worker":
                    value[field]["observation_identity"] = canonical_identity(
                        "other"
                    ).uri
                elif field == "deadline":
                    value[field]["expires_at"] = (
                        self.deadline.expires_at + timedelta(seconds=1)
                    ).isoformat(timespec="microseconds")
                else:
                    value[field] = canonical_identity("changed").uri
                with self.assertRaises(ActionWireError):
                    decode_action_request(canonical_json_bytes(value))
        with self.assertRaises(ActionWireError):
            decode_action_request(encoded.replace(b'"slot":0', b'"slot":0,"slot":0'))

    def test_expired_and_unbounded_deadlines_refuse_before_worker_execution(self):
        encoded = encode_action_request(self.request, self.deadline, self.records)
        with self.assertRaisesRegex(ActionWireError, "expired"):
            decode_action_request(encoded, now=self.deadline.expires_at)
        with self.assertRaisesRegex(ActionWireError, "one day"):
            decode_action_request(
                encoded, now=self.deadline.expires_at - timedelta(days=2)
            )

    def test_result_replay_corruption_and_unsafe_failure_codes_refuse(self):
        wire = encode_action_response(self.request, result_record=b"result")
        for field in (
            "request_identity",
            "worker_identity",
            "result_identity",
            "failure_code",
        ):
            with self.subTest(field=field):
                value = json.loads(wire)
                value[field] = canonical_identity("changed").uri
                with self.assertRaises(ActionWireError):
                    decode_action_response(canonical_json_bytes(value), self.request)
        with self.assertRaises(ActionWireError):
            encode_action_response(self.request, failure_code="secret/path")
        outcome, record = decode_action_response(
            encode_action_response(self.request, failure_code="build.failed"),
            self.request,
        )
        self.assertEqual(outcome.failure_code, "build.failed")
        self.assertIsNone(record)


_WORKER = """
import json, os, pathlib, subprocess, sys, time
from literate_ai.adapters.action_dispatch_wire import (
    decode_action_request, encode_action_response, record_identity,
)
from literate_ai.contracts.identity import canonical_json_bytes
mode = sys.argv[1]
wire = (
    pathlib.Path(sys.argv[2]).read_bytes()
    if len(sys.argv) > 2 else sys.stdin.buffer.read()
)
request, deadline, records = decode_action_request(wire)
if mode == "sleep":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    pathlib.Path("started.json").write_text(json.dumps([os.getpid(), child.pid]))
    time.sleep(60)
if mode == "noisy":
    sys.stderr.write("private-test-secret" * 10000)
    sys.stderr.flush()
    time.sleep(60)
if mode == "barrier":
    pathlib.Path(request.worker.worker_id + ".ready").touch()
    while len(list(pathlib.Path.cwd().glob("*.ready"))) < 2:
        deadline.remaining()
        time.sleep(0.02)
result = canonical_json_bytes({
    "pid": os.getpid(),
    "input": record_identity(records[request.action.payload_identity]).uri,
    "ambient_secret": "UNDECLARED_TEST_SECRET" in os.environ,
    "bound_token": os.environ.get("WORKER_TOKEN"),
})
response = encode_action_response(request, result_record=result)
if mode == "corrupt":
    value = json.loads(response)
    value["result_identity"] = record_identity(b"foreign").uri
    response = canonical_json_bytes(value)
sys.stdout.buffer.write(response)
"""


class CommandActionDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.script = self.root / "worker.py"
        self.script.write_text(_WORKER, encoding="utf-8")
        self.results = {}
        self.admissions = []

    def dispatcher(self, mode="ok", *, request_file=False, duration=60):
        command = (sys.executable, str(self.script), mode)
        if request_file:
            command += ("{request_file}",)
        worker = ExecutionWorker(
            "command",
            ExecutionWorkerKind.COMMAND,
            command=command,
            environment=(
                ExecutionWorkerEnvironment("WORKER_TOKEN", "PRIVATE_TEST_TOKEN", True),
            ),
        )
        catalog = ExecutionWorkerCatalog((worker,))
        deadline = ActionDispatchDeadline(
            datetime.now(UTC) + timedelta(seconds=duration)
        )
        request, deadline, records = request_fixture(worker, catalog, deadline)
        dispatcher = CommandLifecycleActionDispatcher(
            catalog,
            (request.worker,),
            deadline,
            cwd=self.root,
            input_records=lambda _request: records,
            record_result=lambda identity, content: self.results.update(
                {identity: content}
            ),
            revalidate_worker=self.admissions.append,
            environment={
                **os.environ,
                "PRIVATE_TEST_TOKEN": "synthetic-token",
                "UNDECLARED_TEST_SECRET": "must-not-leak",
            },
        )
        return dispatcher, request

    def test_real_worker_process_and_both_input_modes_preserve_exact_custody(self):
        for request_file in (False, True):
            with self.subTest(request_file=request_file):
                dispatcher, request = self.dispatcher(request_file=request_file)
                outcome = dispatcher.dispatch(request)
                content = self.results[outcome.result_identity]
                result = json.loads(content)
                self.assertNotEqual(result["pid"], os.getpid())
                self.assertEqual(result["input"], request.action.payload_identity.uri)
                self.assertFalse(result["ambient_secret"])
                self.assertEqual(result["bound_token"], "synthetic-token")
                self.assertEqual(record_identity(content), outcome.result_identity)
        self.assertEqual(len(self.admissions), 4)

    def test_corrupt_worker_result_never_enters_result_store(self):
        dispatcher, request = self.dispatcher("corrupt")
        with self.assertRaisesRegex(ActionWireError, "content identity"):
            dispatcher.dispatch(request)
        self.assertEqual(self.results, {})

    def test_large_request_uses_explicit_bounded_input_allowance(self):
        dispatcher, request = self.dispatcher()
        payload = b"x" * (128 * 1024)
        records = dict(dispatcher.input_records(request))
        del records[request.action.payload_identity]
        records[record_identity(payload)] = payload
        request = replace(
            request,
            action=replace(request.action, payload_identity=record_identity(payload)),
        )
        dispatcher.input_records = lambda _request: records
        outcome = dispatcher.dispatch(request)
        self.assertEqual(
            json.loads(self.results[outcome.result_identity])["input"],
            record_identity(payload).uri,
        )

    def test_scheduler_uses_two_real_command_worker_slots_concurrently(self):
        workers = tuple(
            ExecutionWorker(
                name,
                ExecutionWorkerKind.COMMAND,
                command=(sys.executable, str(self.script), "barrier"),
            )
            for name in ("first", "second")
        )
        catalog = ExecutionWorkerCatalog(workers)
        deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(seconds=60))
        base, _, records = request_fixture(workers[0], catalog, deadline)
        admitted = tuple(
            LifecycleActionWorker(
                worker.worker_id,
                worker.identity,
                catalog.identity,
                canonical_identity("observation"),
            )
            for worker in workers
        )
        nodes = tuple(
            replace(
                base.action,
                action_id=f"{name}/index",
                component_revision=canonical_identity(name),
                predecessor_ids=(),
                eligible_worker_ids=("first", "second"),
            )
            for name in ("alpha", "beta")
        )
        dispatcher = CommandLifecycleActionDispatcher(
            catalog,
            admitted,
            deadline,
            cwd=self.root,
            input_records=lambda request: {
                request.action.payload_identity: records[
                    request.action.payload_identity
                ]
            },
            record_result=lambda identity, content: self.results.update(
                {identity: content}
            ),
            revalidate_worker=self.admissions.append,
        )
        result = LifecycleActionDagScheduler().run(
            nodes, admitted, dispatcher, deadline_identity=deadline.identity
        )
        self.assertEqual(
            {item.disposition.value for item in result.results},
            {"accepted"},
            tuple((item.action_id, item.failure_code) for item in result.results),
        )
        self.assertEqual(len(self.results), 2)
        self.assertEqual(
            len({json.loads(record)["pid"] for record in self.results.values()}), 2
        )

    def test_worker_drift_after_execution_prevents_result_admission(self):
        dispatcher, request = self.dispatcher()

        def revalidate(worker):
            if self.admissions:
                raise ActionWireError(
                    "action_dispatch.admission_changed", "worker admission changed"
                )
            self.admissions.append(worker)

        dispatcher.revalidate_worker = revalidate
        with self.assertRaisesRegex(ActionWireError, "admission changed"):
            dispatcher.dispatch(request)
        self.assertEqual(self.results, {})

    def test_deadline_terminates_a_running_worker_without_result(self):
        dispatcher, request = self.dispatcher("sleep", duration=2)
        with self.assertRaisesRegex(ActionWireError, "expired"):
            dispatcher.dispatch(request)
        self.assertEqual(self.results, {})

    def test_changed_admission_or_missing_credential_prevents_execution(self):
        dispatcher, request = self.dispatcher()
        changed = replace(
            request,
            worker=replace(
                request.worker, observation_identity=canonical_identity("stale")
            ),
        )
        with self.assertRaisesRegex(ActionWireError, "admitted worker"):
            dispatcher.dispatch(changed)
        del dispatcher.environment["PRIVATE_TEST_TOKEN"]
        with self.assertRaisesRegex(ActionWireError, "environment binding"):
            dispatcher.dispatch(request)
        self.assertEqual(self.results, {})

    def test_output_overflow_is_bounded_and_diagnostics_do_not_expose_stderr(self):
        dispatcher, request = self.dispatcher("noisy")
        with self.assertRaises(ActionWireError) as caught:
            dispatcher.dispatch(request)
        self.assertEqual(caught.exception.code, "action_dispatch.output_oversized")
        self.assertNotIn("private-test-secret", str(caught.exception))
        self.assertEqual(self.results, {})

    def test_deadline_and_pre_dispatch_cancellation_prevent_admission(self):
        dispatcher, request = self.dispatcher(duration=-1)
        with self.assertRaisesRegex(ActionWireError, "expired"):
            dispatcher.dispatch(request)
        dispatcher, request = self.dispatcher()
        dispatcher.cancel(request)
        with self.assertRaisesRegex(ActionWireError, "cancelled"):
            dispatcher.dispatch(request)
        self.assertEqual(self.admissions, [])
        self.assertEqual(self.results, {})

    def test_cancellation_terminates_owned_process_tree_without_result(self):
        dispatcher, request = self.dispatcher("sleep")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(dispatcher.dispatch, request)
            marker = self.root / "started.json"
            end = time.monotonic() + 15
            while not marker.exists() and time.monotonic() < end and not future.done():
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "worker did not start")
            dispatcher.cancel(request)
            with self.assertRaisesRegex(ActionWireError, "cancelled"):
                future.result(timeout=10)
        self.assertEqual(self.results, {})
        if os.name == "posix":
            root_pid, child_pid = json.loads(marker.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(root_pid, 0)
            # A reparented child may briefly be a zombie; it must not be running.
            observed = __import__("subprocess").run(
                ["ps", "-o", "stat=", "-p", str(child_pid)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertTrue(
                not observed.stdout.strip() or observed.stdout.strip().startswith("Z")
            )
