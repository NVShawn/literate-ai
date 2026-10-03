"""Unsuccessful observations expose only closed symbolic and numeric diagnostics."""

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.action_worker import main
from literate_ai.adapters.action_capabilities import run_command_observation
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.action_observation_failure import (
    encode_observation_failure,
    observation_failure_message,
)
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.worker_tool_dependencies import (
    WorkerDependencyGraphLimitError,
)
from literate_ai.contracts import canonical_json_bytes
from literate_ai.contracts.execution_dispatch import (
    LIFECYCLE_ACTION_WIRE_PROTOCOL,
    ExecutionWorker,
    ExecutionWorkerKind,
)
from literate_ai.storage import FileSystemCAS


class ObservationFailureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cas = FileSystemCAS(self.root / "cas")
        self.worker = ExecutionWorker(
            "worker",
            ExecutionWorkerKind.COMMAND,
            command=(sys.executable, "-c", "pass"),
            action_protocol=LIFECYCLE_ACTION_WIRE_PROTOCOL,
        )
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(minutes=1))
        self.request = canonical_json_bytes(
            {
                "schema": "literate-ai/action-capability-request@1",
                "worker_identity": self.worker.identity.uri,
                "nonce": "a" * 32,
                "deadline": self.deadline.to_dict(),
            }
        )

    def receive(self, error):
        output = io.BytesIO()
        with (
            patch(
                "literate_ai.action_worker.sys.stdin",
                SimpleNamespace(buffer=io.BytesIO(self.request)),
            ),
            patch(
                "literate_ai.action_worker.sys.stdout", SimpleNamespace(buffer=output)
            ),
            patch("literate_ai.action_worker.sys.stderr", io.StringIO()),
            patch.dict(
                os.environ, LITAI_ACTION_WORKER_IDENTITY=self.worker.identity.uri
            ),
            patch(
                "literate_ai.action_worker.encode_capability_response",
                side_effect=error,
            ),
        ):
            code = main(
                [
                    "--describe",
                    "--cas",
                    str(self.cas.root),
                    "--workspace",
                    str(self.root),
                ]
            )
        self.assertEqual(code, 2)
        return output.getvalue()

    def test_receiver_emits_graph_sizes_without_exception_or_graph_contents(self):
        content = self.receive(
            WorkerDependencyGraphLimitError(
                size=5000000,
                limit=4194304,
                components=900,
                edges=1200,
                component_bytes=4800000,
                edge_bytes=200000,
            )
        )
        value = json.loads(content)
        self.assertEqual(value["code"], "action_dependencies.graph_limit")
        self.assertEqual(value["metrics"]["bytes"], 5000000)
        self.assertLess(len(content), 2048)
        self.assertIn("components=900", observation_failure_message(content))

    def test_receiver_discards_arbitrary_exception_text(self):
        for error, expected in (
            (
                ActionWireError("action_wire.expired", "private host secret"),
                "action_wire.expired",
            ),
            (
                ValueError("private host secret"),
                "action_capability.custody_unavailable",
            ),
            (
                ActionWireError("private host secret", "private host secret"),
                "action_capability.custody_unavailable",
            ),
        ):
            with self.subTest(error=type(error).__name__):
                content = self.receive(error)
                self.assertEqual(json.loads(content)["code"], expected)
                self.assertNotIn(b"private", content)

    def test_nonzero_transport_preserves_safe_reason_but_never_admits_success(self):
        content = encode_observation_failure("action_wire.expired")
        with patch(
            "literate_ai.adapters.action_capabilities.run_bounded_process",
            return_value=BoundedProcessResult(2, content, b"private stderr"),
        ):
            with self.assertRaises(ActionWireError) as error:
                self.observe()
        self.assertEqual(error.exception.code, "action_capability.probe_failed")
        self.assertIn("action_wire.expired", str(error.exception))
        self.assertNotIn("private", str(error.exception))

    def observe(self):
        return run_command_observation(
            self.worker,
            self.deadline,
            self.request,
            cwd=self.root,
            environment=dict(os.environ),
            mode="--describe",
            protocol="literate-ai/action-capabilities@1",
        )

    def test_legacy_malformed_or_unbounded_diagnostics_remain_opaque(self):
        base = json.loads(encode_observation_failure("action_wire.expired"))
        malformed = (
            b"private stderr",
            b"x" * 2049,
            canonical_json_bytes(base | {"private_path": "secret"}),
            canonical_json_bytes(base | {"metrics": {"bytes": True}}),
            canonical_json_bytes(base | {"metrics": {"bytes": -1}}),
            json.dumps(base | {"metrics": {"bytes": 2**63}}).encode(),
            canonical_json_bytes(base | {"metrics": {"path": "secret"}}),
            canonical_json_bytes(base | {"code": "private host secret"}),
            (
                b'{"code":"hidden","code":"action_wire.expired","metrics":{},'
                b'"schema":"literate-ai/action-observation-failure@1"}'
            ),
        )
        for content in malformed:
            with self.subTest(content=content[:40]):
                self.assertIsNone(observation_failure_message(content))
                with patch(
                    "literate_ai.adapters.action_capabilities.run_bounded_process",
                    return_value=BoundedProcessResult(2, content, b"private stderr"),
                ):
                    with self.assertRaises(ActionWireError) as error:
                        self.observe()
                self.assertEqual(
                    str(error.exception), "worker capability probe refused the request"
                )
