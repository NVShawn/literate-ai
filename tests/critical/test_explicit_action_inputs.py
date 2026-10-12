"""Exact data custody and real command-process lifecycle-action transport."""

from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    decode_action_request,
    encode_action_request,
    record_identity,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
    LifecycleActionNode,
    LifecycleActionWorker,
)
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerCatalog,
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

    def test_explicit_inputs_preserve_predecessor_custody_and_legacy_identity(self):
        request = self.request
        legacy = canonical_identity(
            {
                "schema": "literate-ai/lifecycle-action-dispatch-request@1",
                "schedule_identity": request.schedule_identity.uri,
                "action_identity": request.action.identity.uri,
                "worker_identity": request.worker.identity.uri,
                "slot": request.slot,
                "predecessor_result_identities": [
                    item.uri for item in request.predecessor_result_identities
                ],
                "deadline_identity": request.deadline_identity.uri,
            }
        )
        self.assertEqual(request.identity, legacy)
        old = json.loads(encode_action_request(request, self.deadline, self.records))
        self.assertTrue(old["schema"].endswith("@1"))
        self.assertNotIn("input_record_identities", old)
        extra = b"explicit bounded request"
        identity = record_identity(extra)
        extended = replace(request, input_record_identities=(identity,))
        records = self.records | {identity: extra}
        encoded = encode_action_request(extended, self.deadline, records)
        self.assertTrue(json.loads(encoded)["schema"].endswith("@2"))
        self.assertEqual(
            decode_action_request(encoded), (extended, self.deadline, records)
        )
        with self.assertRaises(ActionWireError):
            encode_action_request(extended, self.deadline, self.records)
        with self.assertRaises(ActionWireError):
            encode_action_request(
                replace(extended, predecessor_result_identities=()),
                self.deadline,
                records,
            )
        forged = json.loads(encoded)
        forged["input_record_identities"] = []
        with self.assertRaises(ActionWireError):
            decode_action_request(canonical_json_bytes(forged))


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
