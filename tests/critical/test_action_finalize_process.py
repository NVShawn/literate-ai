"""Actual FINALIZE child bounds, controls, cancellation and private revocation."""

import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import tests.support.fixtures_test_action_finalize_record as fixture_module
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_finalize_process import run_finalize_worker_process
from literate_ai.adapters.lifecycle import LocalComponentToolBinding


class FinalizeProcessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.FinalizeWorkerInputTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.raw = f.value.to_bytes()
        self.authority = Mock()

    def run_child(self, code, **changes):
        arguments = dict(
            launcher=LocalComponentToolBinding(sys.executable, ("-c", code)),
            input_record=self.raw,
            input_identity=record_identity(self.raw),
            deadline=self.fixture.deadline,
            cwd=self.root,
            environment=dict(os.environ),
            require_execution_authority=self.authority,
            cas_root=self.root,
            workspace_root=self.root,
        )
        arguments.update(changes)
        return run_finalize_worker_process(**arguments)

    def test_exact_input_authority_and_reserved_controls(self):
        result = json.loads(
            self.run_child(
                "import sys,os,json; print(json.dumps(["
                "sys.stdin.buffer.read().decode(),{k:v for k,v in os.environ.items() "
                "if k.lower().startswith('litai_finalize_')}]))",
                environment=dict(
                    os.environ,
                    litai_finalize_cas="foreign",
                    LITAI_FINALIZE_INPUT_IDENTITY="foreign",
                ),
            )
        )
        self.assertEqual(result[0], self.raw.decode())
        self.assertEqual(
            result[1],
            {
                "LITAI_FINALIZE_INPUT_IDENTITY": record_identity(self.raw).uri,
                "LITAI_FINALIZE_DEADLINE": self.fixture.deadline.expires_at.isoformat(),
                "LITAI_FINALIZE_CAS": str(self.root),
                "LITAI_FINALIZE_WORKSPACE": str(self.root),
            },
        )
        self.assertGreaterEqual(self.authority.call_count, 2)
        for call in self.authority.call_args_list:
            self.assertEqual(call.args, (self.fixture.value,))

    def test_invalid_or_unauthorized_input_never_launches(self):
        with patch(
            "literate_ai.adapters.action_worker_process.run_bounded_process",
            side_effect=AssertionError("launched"),
        ):
            for changes, error in (
                ({"input_record": b"bad"}, ActionWireError),
                ({"cancelled": lambda: True}, ActionWireError),
                ({"cas_root": Path("relative")}, ActionWireError),
                ({"require_execution_authority": None}, TypeError),
                (
                    {"require_execution_authority": Mock(side_effect=PermissionError)},
                    PermissionError,
                ),
            ):
                with self.subTest(fields=tuple(changes)), self.assertRaises(error):
                    self.run_child("pass", **changes)

    def test_revocation_interrupts_running_child(self):
        marker = self.root / "started"

        def authority(value):
            self.assertEqual(value, self.fixture.value)
            if marker.exists():
                raise PermissionError("revoked")

        with self.assertRaisesRegex(PermissionError, "revoked"):
            self.run_child(
                "import sys,time; from pathlib import Path; sys.stdin.buffer.read(); "
                f"Path({str(marker)!r}).touch(); time.sleep(30)",
                require_execution_authority=authority,
            )

    def test_cancellation_interrupts_running_child(self):
        marker = self.root / "started"
        with self.assertRaises(ActionWireError) as error:
            self.run_child(
                "import sys,time; from pathlib import Path; sys.stdin.buffer.read(); "
                f"Path({str(marker)!r}).touch(); time.sleep(30)",
                cancelled=marker.exists,
            )
        self.assertEqual(error.exception.code, "action_finalize.cancelled")

    def test_failed_child_cannot_return_success(self):
        with self.assertRaises(ActionWireError) as error:
            self.run_child("import sys; sys.stdin.buffer.read(); raise SystemExit(2)")
        self.assertEqual(error.exception.code, "action_finalize.process_failed")

    def test_output_bound_refuses_child_result(self):
        with self.assertRaises(ActionWireError) as error:
            self.run_child(
                "import sys; sys.stdin.buffer.read(); "
                f"sys.stdout.buffer.write(b'x'*{MAX_ACTION_RECORD_BYTES + 1})"
            )
        self.assertEqual(error.exception.code, "action_finalize.output_oversized")

    def test_deadline_stops_running_child(self):
        marker = self.root / "started"
        with self.assertRaises(ActionWireError) as error:
            self.run_child(
                "import sys,time; from pathlib import Path; sys.stdin.buffer.read(); "
                f"Path({str(marker)!r}).touch(); time.sleep(30)",
                deadline=ActionDispatchDeadline(
                    datetime.now(UTC) + timedelta(seconds=2)
                ),
            )
        self.assertTrue(marker.exists())
        self.assertIn(
            error.exception.code, {"action_finalize.expired", "action_wire.expired"}
        )
