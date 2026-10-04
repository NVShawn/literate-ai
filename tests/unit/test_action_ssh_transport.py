"""SSH argument custody and real receiver execution through the transport seam."""

from __future__ import annotations

import base64
import os
import shlex
import shutil
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from literate_ai.adapters.action_capabilities import probe_command_action_capabilities
from literate_ai.adapters.action_command_dispatch import (
    CommandLifecycleActionDispatcher,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.action_transport import (
    action_receiver_command,
    supports_action_transport,
)
from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.command_indexer import CommandGenerationIndexer
from literate_ai.adapters.lifecycle.standard_local import LocalSourceTreeRegistry
from literate_ai.contracts import ContractValidationError
from literate_ai.contracts.execution_dispatch import (
    LIFECYCLE_ACTION_WIRE_PROTOCOL,
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
)
from literate_ai.contracts.identity import canonical_identity
from tests.support import fixtures_test_action_source_index as source_fixture
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


class SshActionTransportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixture.SourceIndexActionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.worker = ExecutionWorker(
            "index",
            ExecutionWorkerKind.SSH,
            requirements=ExecutionRequirements(
                os_family="windows" if os.name == "nt" else "linux"
            ),
            endpoint="runner@example.invalid",
            workspace="~/work",
            action_protocol=LIFECYCLE_ACTION_WIRE_PROTOCOL,
            action_command=self.fixture.worker.command,
        )
        self.deadline = ActionDispatchDeadline(
            datetime.now(UTC) + timedelta(seconds=120)
        )
        self.fixture.deadline = self.deadline

    def test_contract_preserves_legacy_identity_and_validates_opt_in(self):
        legacy = replace(self.worker, action_protocol=None, action_command=())
        self.assertNotIn("action_command", legacy.to_dict())
        self.assertFalse(supports_action_transport(legacy))
        self.assertEqual(ExecutionWorker.from_dict(legacy.to_dict()), legacy)
        self.assertEqual(ExecutionWorker.from_dict(self.worker.to_dict()), self.worker)
        SchemaCatalog().validate(self.worker.SCHEMA, self.worker.to_dict())
        for values in (
            {"action_protocol": None},
            {"action_command": ["receiver"]},
            {"action_command": ("receiver", "line\nbreak")},
            {"action_command": ("receiver", "{request_file}")},
        ):
            with (
                self.subTest(values=values),
                self.assertRaises(ContractValidationError),
            ):
                replace(self.worker, **values)
        with self.assertRaises(ActionWireError):
            action_receiver_command(legacy, self.deadline)

    def test_posix_arguments_remain_literal_and_payload_is_not_in_argv(self):
        values = ("receiver", "path with spaces", "$(touch escaped)", "it's literal")
        worker = replace(
            self.worker,
            action_command=values,
            requirements=ExecutionRequirements(os_family="linux"),
        )
        argv = action_receiver_command(worker, self.deadline, mode="--describe")
        shell = shlex.split(argv[-1])
        self.assertEqual(shell[:2], ["bash", "-lic"])
        self.assertEqual(shlex.split(shell[2]), ["exec", *values, "--describe"])
        self.assertEqual(argv[-2], worker.endpoint)

    def test_windows_uses_literal_encoded_native_invocation_and_bounds(self):
        worker = replace(
            self.worker,
            requirements=ExecutionRequirements(os_family="windows"),
            action_command=("C:/receiver.exe", "it's $literal; $(value)"),
        )
        command = action_receiver_command(worker, self.deadline)[-1]
        decoded = base64.b64decode(command.split()[-1]).decode("utf-16-le")
        self.assertEqual(
            decoded,
            "& 'C:/receiver.exe' 'it''s $literal; $(value)'; exit $LASTEXITCODE",
        )
        oversized = replace(worker, action_command=("receiver", "a" * 4096))
        with self.assertRaises(ActionWireError) as caught:
            action_receiver_command(oversized, self.deadline)
        self.assertEqual(caught.exception.code, "action_transport.command_oversized")

    def _native_receiver(self, argv, **kwargs):
        # Substitute only the SSH network hop. Execute its actual generated shell
        # command and receiver process; the remote private binding is independently
        # configured here rather than forwarded in the controller environment.
        self.assertEqual(argv[-2], self.worker.endpoint)
        self.assertNotIn("LITAI_ACTION_WORKER_IDENTITY", kwargs["environment"])
        self.assertNotIn("CONTROLLER_SECRET", kwargs["environment"])
        kwargs["environment"] = {
            **os.environ,
            "LITAI_ACTION_WORKER_IDENTITY": self.worker.identity.uri,
        }
        return run_bounded_process(tuple(shlex.split(argv[-1])), **kwargs)

    @unittest.skipUnless(
        os.name == "nt" or shutil.which("bash"), "native receiver shell required"
    )
    def test_wrong_worker_and_failed_receiver_are_not_admitted(self):
        for command in (
            self.worker.action_command,
            (self.worker.action_command[0], "-c", "raise SystemExit(23)"),
        ):
            worker = replace(self.worker, action_command=command)

            def wrong_receiver(argv, worker=worker, **kwargs):
                kwargs["environment"] = {
                    **os.environ,
                    "LITAI_ACTION_WORKER_IDENTITY": replace(
                        worker, worker_id="different-worker"
                    ).identity.uri,
                }
                return run_bounded_process(tuple(shlex.split(argv[-1])), **kwargs)

            with (
                self.subTest(command=command),
                patch(
                    "literate_ai.adapters.action_capabilities.run_bounded_process",
                    side_effect=wrong_receiver,
                ),
                self.assertRaises(ActionWireError),
            ):
                probe_command_action_capabilities(
                    worker, self.deadline, cwd=self.fixture.root
                )

    @unittest.skipUnless(
        os.name == "nt" or shutil.which("bash"), "native receiver shell required"
    )
    def test_real_capability_and_index_receivers_preserve_request_custody(self):
        fixture = self.fixture
        fixture.worker = self.worker
        fixture.catalog = ExecutionWorkerCatalog((self.worker,))
        fixture.fixture({"source/main.py": b"print('ssh receiver')\n"})
        environment = {**os.environ, "CONTROLLER_SECRET": "must-not-forward"}
        with patch(
            "literate_ai.adapters.action_capabilities.run_bounded_process",
            side_effect=self._native_receiver,
        ):
            observed = probe_command_action_capabilities(
                self.worker, self.deadline, cwd=fixture.root, environment=environment
            )
        self.assertEqual(observed.worker_identity, self.worker.identity)
        dispatcher = CommandLifecycleActionDispatcher(
            fixture.catalog,
            (fixture.request.worker,),
            self.deadline,
            cwd=fixture.root,
            input_records=lambda request: fixture.records,
            record_result=lambda identity, content: fixture.results.update(
                {identity: content}
            ),
            revalidate_worker=lambda worker: None,
            environment=environment,
        )
        with patch(
            "literate_ai.adapters.action_command_dispatch.run_bounded_process",
            side_effect=self._native_receiver,
        ):
            outcome = dispatcher.dispatch(fixture.request)
        self.assertIn(outcome.result_identity, fixture.results)
        self.assertFalse(tuple(fixture.workspace.iterdir()))
        source = fixture.root / "generated"
        source.mkdir()
        for item in fixture.files:
            path = source / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(fixture.cas.get_bytes(item.blob))
        registry = LocalSourceTreeRegistry()
        registry.register(fixture.candidate, source)
        indexer = CommandGenerationIndexer(
            fixture.execution_plan,
            registry,
            lambda identity: fixture.candidate,
            fixture.cas,
            fixture.catalog,
            (fixture.request.worker,),
            self.deadline,
            cwd=fixture.root,
            revalidate_worker=lambda worker: None,
            environment=environment,
        )
        with patch(
            "literate_ai.adapters.action_command_dispatch.run_bounded_process",
            side_effect=self._native_receiver,
        ):
            result = indexer.index(
                fixture.candidate.component_revision, fixture.candidate.tree_identity
            )
        self.assertEqual(result, canonical_identity(fixture.expected_result()))
