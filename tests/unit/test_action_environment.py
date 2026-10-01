"""Native environment name semantics without widening action-worker forwarding."""

import unittest
from unittest.mock import patch

from literate_ai.adapters.action_command_dispatch import command_worker_environment
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerEnvironment,
    ExecutionWorkerKind,
)


class ActionEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.worker = ExecutionWorker(
            "receiver",
            ExecutionWorkerKind.COMMAND,
            command=("receiver",),
            environment=(ExecutionWorkerEnvironment("TOKEN", "PRIVATE_TOKEN", True),),
        )

    def test_windows_runtime_and_explicit_bindings_ignore_name_case(self):
        bindings = {
            "path": "runtime-bin",
            "SYSTEMROOT": "windows-runtime",
            "COMSPEC": "command-shell",
            "private_token": "declared-token",
            "UNDECLARED_SECRET": "must-not-forward",
        }
        with patch("literate_ai.adapters.action_command_dispatch.os.name", "nt"):
            result = command_worker_environment(self.worker, bindings)
        self.assertEqual(result["SystemRoot"], "windows-runtime")
        self.assertEqual(result["ComSpec"], "command-shell")
        self.assertEqual(result["PATH"], "runtime-bin")
        self.assertEqual(result["TOKEN"], "declared-token")
        self.assertEqual(
            set(result),
            {"SystemRoot", "ComSpec", "PATH", "TOKEN", "LITAI_DISPATCH_PROTOCOL"},
        )

    def test_posix_binding_names_remain_case_sensitive(self):
        with patch("literate_ai.adapters.action_command_dispatch.os.name", "posix"):
            with self.assertRaises(ActionWireError):
                command_worker_environment(self.worker, {"private_token": "wrong-case"})
            result = command_worker_environment(
                self.worker, {"PATH": "runtime-bin", "PRIVATE_TOKEN": "declared-token"}
            )
        self.assertEqual(result["TOKEN"], "declared-token")
